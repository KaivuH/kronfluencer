from typing import Tuple

import torch
import torch.distributed as dist
import torch.nn as nn

from kronfluence.factor.config import FactorConfig
from kronfluence.module.tracker.base import BaseTracker
from kronfluence.utils.constants import (
    ACTIVATION_COVARIANCE_MATRIX_NAME,
    ACTIVATION_EIGENVECTORS_NAME,
    COVARIANCE_FACTOR_NAMES,
    EIGENDECOMPOSITION_FACTOR_NAMES,
    GRADIENT_COVARIANCE_MATRIX_NAME,
    GRADIENT_EIGENVECTORS_NAME,
    LAMBDA_FACTOR_NAMES,
    LAMBDA_MATRIX_NAME,
    NUM_ACTIVATION_COVARIANCE_PROCESSED,
    NUM_GRADIENT_COVARIANCE_PROCESSED,
    NUM_LAMBDA_PROCESSED,
)
from kronfluence.utils.exceptions import FactorsNotFoundError


class CovarianceTracker(BaseTracker):
    """Tracks and computes activation and gradient covariance matrices for a given module."""

    def _update_activation_covariance_matrix(self, input_activation: torch.Tensor) -> None:
        """Computes and updates the activation covariance matrix.

        Args:
            input_activation (torch.Tensor):
                The input tensor to the module, provided by PyTorch's forward hook.
        """
        flattened_activation, count = self.module.get_flattened_activation(input_activation=input_activation)

        if self.module.storage[NUM_ACTIVATION_COVARIANCE_PROCESSED] is None:
            self.module.storage[NUM_ACTIVATION_COVARIANCE_PROCESSED] = torch.zeros(
                size=(1,),
                dtype=torch.int64,
                device=count.device if isinstance(count, torch.Tensor) else None,
                requires_grad=False,
            )
            dimension = flattened_activation.size(1)
            self.module.storage[ACTIVATION_COVARIANCE_MATRIX_NAME] = torch.zeros(
                size=(dimension, dimension),
                dtype=flattened_activation.dtype,
                device=flattened_activation.device,
                requires_grad=False,
            )
        self.module.storage[NUM_ACTIVATION_COVARIANCE_PROCESSED].add_(count)
        self.module.storage[ACTIVATION_COVARIANCE_MATRIX_NAME].addmm_(flattened_activation.t(), flattened_activation)

    def _update_gradient_covariance_matrix(self, output_gradient: torch.Tensor) -> None:
        """Computes and updates the pseudo-gradient covariance matrix.

        Args:
            output_gradient (torch.Tensor):
                The gradient tensor with respect to the output of the module, provided by PyTorch's backward hook.
        """
        flattened_gradient, count = self.module.get_flattened_gradient(output_gradient=output_gradient)

        if self.module.storage[NUM_GRADIENT_COVARIANCE_PROCESSED] is None:
            # In most cases, `NUM_GRADIENT_COVARIANCE_PROCESSED` and `NUM_ACTIVATION_COVARIANCE_PROCESSED` are
            # identical. However, they may differ when using gradient checkpointing or torch.compile().
            self.module.storage[NUM_GRADIENT_COVARIANCE_PROCESSED] = torch.zeros(
                size=(1,),
                dtype=torch.int64,
                device=count.device if isinstance(count, torch.Tensor) else None,
                requires_grad=False,
            )
            dimension = flattened_gradient.size(1)
            self.module.storage[GRADIENT_COVARIANCE_MATRIX_NAME] = torch.zeros(
                size=(dimension, dimension),
                dtype=flattened_gradient.dtype,
                device=flattened_gradient.device,
                requires_grad=False,
            )
        self.module.storage[NUM_GRADIENT_COVARIANCE_PROCESSED].add_(count)
        self.module.storage[GRADIENT_COVARIANCE_MATRIX_NAME].addmm_(flattened_gradient.t(), flattened_gradient)

    def register_hooks(self) -> None:
        """Sets up hooks to compute activation and gradient covariance matrices."""

        @torch.no_grad()
        def forward_hook(module: nn.Module, inputs: Tuple[torch.Tensor], outputs: torch.Tensor) -> None:
            del module
            # Computes and updates activation covariance during forward pass.
            input_activation = inputs[0].detach().to(dtype=self.module.factor_args.activation_covariance_dtype)
            self._update_activation_covariance_matrix(input_activation=input_activation)
            outputs.register_hook(backward_hook)

        @torch.no_grad()
        def backward_hook(output_gradient: torch.Tensor) -> None:
            # Computes and updates pseudo-gradient covariance during backward pass.
            output_gradient = self._scale_output_gradient(
                output_gradient=output_gradient, target_dtype=self.module.factor_args.gradient_covariance_dtype
            )
            self._update_gradient_covariance_matrix(output_gradient=output_gradient)

        self.registered_hooks.append(self.module.original_module.register_forward_hook(forward_hook))

    def exist(self) -> bool:
        """Checks if both activation and gradient covariance matrices are available."""
        for covariance_factor_name in COVARIANCE_FACTOR_NAMES:
            if self.module.storage[covariance_factor_name] is None:
                return False
        return True

    def synchronize(self, num_processes: int) -> None:
        """Aggregates covariance matrices across multiple devices or nodes in a distributed setting."""
        del num_processes
        if dist.is_initialized() and torch.cuda.is_available() and self.exist():
            for covariance_factor_name in COVARIANCE_FACTOR_NAMES:
                self.module.storage[covariance_factor_name] = self.module.storage[covariance_factor_name].cuda()
                dist.reduce(
                    tensor=self.module.storage[covariance_factor_name],
                    op=dist.ReduceOp.SUM,
                    dst=0,
                )

    def release_memory(self) -> None:
        """Clears all covariance matrices from memory."""
        for covariance_factor_name in COVARIANCE_FACTOR_NAMES:
            del self.module.storage[covariance_factor_name]
            self.module.storage[covariance_factor_name] = None


class LambdaTracker(BaseTracker):
    """Tracks and computes Lambda matrices for a given module."""

    def _eigendecomposition_results_exist(self) -> bool:
        """Checks if eigendecomposition results are available."""
        for eigen_factor_name in EIGENDECOMPOSITION_FACTOR_NAMES:
            if self.module.storage[eigen_factor_name] is None:
                return False
        return True

    def _update_lambda_matrix(self, per_sample_gradient: torch.Tensor) -> None:
        """Computes and updates the Lambda matrix using provided per-sample gradient.

        Args:
            per_sample_gradient (torch.Tensor):
                The per-sample gradient tensor for the given batch.
        """
        batch_size = per_sample_gradient.size(0)

        if self.module.storage[NUM_LAMBDA_PROCESSED] is None:
            self.module.storage[NUM_LAMBDA_PROCESSED] = torch.zeros(
                size=(1,),
                dtype=torch.int64,
                device=None,
                requires_grad=False,
            )
            self.module.storage[LAMBDA_MATRIX_NAME] = torch.zeros(
                size=(per_sample_gradient.size(1), per_sample_gradient.size(2)),
                dtype=per_sample_gradient.dtype,
                device=per_sample_gradient.device,
                requires_grad=False,
            )

            if FactorConfig.CONFIGS[self.module.factor_args.strategy].requires_eigendecomposition_for_lambda:
                if not self._eigendecomposition_results_exist():
                    raise FactorsNotFoundError(
                        f"The strategy {self.module.factor_args.strategy} requires eigendecomposition "
                        f"results for Lambda computations, but they are not found."
                    )

                # Move activation and pseudo-gradient eigenvectors to appropriate devices.
                self.module.storage[ACTIVATION_EIGENVECTORS_NAME] = self.module.storage[
                    ACTIVATION_EIGENVECTORS_NAME
                ].to(
                    dtype=per_sample_gradient.dtype,
                    device=per_sample_gradient.device,
                )
                self.module.storage[GRADIENT_EIGENVECTORS_NAME] = self.module.storage[GRADIENT_EIGENVECTORS_NAME].to(
                    dtype=per_sample_gradient.dtype,
                    device=per_sample_gradient.device,
                )

        self.module.storage[NUM_LAMBDA_PROCESSED].add_(batch_size)
        if FactorConfig.CONFIGS[self.module.factor_args.strategy].requires_eigendecomposition_for_lambda:
            if self.module.factor_args.use_iterative_lambda_aggregation:
                # This batch-wise iterative update can be useful when the GPU memory is limited.
                per_sample_gradient = torch.matmul(
                    per_sample_gradient,
                    self.module.storage[ACTIVATION_EIGENVECTORS_NAME],
                )
                for i in range(batch_size):
                    sqrt_lambda = torch.matmul(
                        self.module.storage[GRADIENT_EIGENVECTORS_NAME].t(),
                        per_sample_gradient[i],
                    )
                    self.module.storage[LAMBDA_MATRIX_NAME].add_(sqrt_lambda.square_())
            else:
                per_sample_gradient = torch.matmul(
                    self.module.storage[GRADIENT_EIGENVECTORS_NAME].t(),
                    torch.matmul(per_sample_gradient, self.module.storage[ACTIVATION_EIGENVECTORS_NAME]),
                )
                self.module.storage[LAMBDA_MATRIX_NAME].add_(per_sample_gradient.square_().sum(dim=0))
        else:
            # Approximate the eigenbasis as identity.
            self.module.storage[LAMBDA_MATRIX_NAME].add_(per_sample_gradient.square_().sum(dim=0))

    def register_hooks(self) -> None:
        """Sets up hooks to compute lambda matrices."""

        @torch.no_grad()
        def forward_hook(module: nn.Module, inputs: Tuple[torch.Tensor], outputs: torch.Tensor) -> None:
            del module
            cached_activation = inputs[0].detach()
            device = "cpu" if self.module.factor_args.offload_activations_to_cpu else cached_activation.device
            cached_activation = cached_activation.to(
                device=device,
                dtype=self.module.factor_args.per_sample_gradient_dtype,
                copy=True,
            )

            if self.module.factor_args.has_shared_parameters:
                if self.cached_activations is None:
                    self.cached_activations = []
                self.cached_activations.append(cached_activation)
            else:
                self.cached_activations = cached_activation

            outputs.register_hook(
                shared_backward_hook if self.module.factor_args.has_shared_parameters else backward_hook
            )

        @torch.no_grad()
        def backward_hook(output_gradient: torch.Tensor) -> None:
            if self.cached_activations is None:
                self._raise_cache_not_found_exception()

            output_gradient = self._scale_output_gradient(
                output_gradient=output_gradient, target_dtype=self.module.factor_args.per_sample_gradient_dtype
            )
            per_sample_gradient = self.module.compute_per_sample_gradient(
                input_activation=self.cached_activations.to(device=output_gradient.device),
                output_gradient=output_gradient,
            ).to(dtype=self.module.factor_args.lambda_dtype)
            self.clear_all_cache()
            self._update_lambda_matrix(per_sample_gradient=per_sample_gradient)

        @torch.no_grad()
        def shared_backward_hook(output_gradient: torch.Tensor) -> None:
            output_gradient = self._scale_output_gradient(
                output_gradient=output_gradient, target_dtype=self.module.factor_args.per_sample_gradient_dtype
            )
            cached_activation = self.cached_activations.pop()
            per_sample_gradient = self.module.compute_per_sample_gradient(
                input_activation=cached_activation.to(device=output_gradient.device),
                output_gradient=output_gradient,
            )
            if self.cached_per_sample_gradient is None:
                self.cached_per_sample_gradient = torch.zeros_like(per_sample_gradient, requires_grad=False)
            self.cached_per_sample_gradient.add_(per_sample_gradient)

        self.registered_hooks.append(self.module.original_module.register_forward_hook(forward_hook))

    @torch.no_grad()
    def finalize_iteration(self) -> None:
        """Updates Lambda matrix using cached per-sample gradients."""
        self.cached_per_sample_gradient = self.cached_per_sample_gradient.to(dtype=self.module.factor_args.lambda_dtype)
        if self.module.factor_args.has_shared_parameters:
            self._update_lambda_matrix(per_sample_gradient=self.cached_per_sample_gradient)
        self.clear_all_cache()

    def exist(self) -> bool:
        """Checks if Lambda matrices are available."""
        for lambda_factor_name in LAMBDA_FACTOR_NAMES:
            if self.module.storage[lambda_factor_name] is None:
                return False
        return True

    def synchronize(self, num_processes: int) -> None:
        """Aggregates Lambda matrices across multiple devices or nodes in a distributed setting."""
        del num_processes
        if dist.is_initialized() and torch.cuda.is_available() and self.exist():
            for lambda_factor_name in LAMBDA_FACTOR_NAMES:
                self.module.storage[lambda_factor_name] = self.module.storage[lambda_factor_name].cuda()
                dist.reduce(
                    tensor=self.module.storage[lambda_factor_name],
                    op=dist.ReduceOp.SUM,
                    dst=0,
                )

    def release_memory(self) -> None:
        """Clears Lambda matrices from memory."""
        self.clear_all_cache()
        for lambda_factor_name in LAMBDA_FACTOR_NAMES:
            del self.module.storage[lambda_factor_name]
            self.module.storage[lambda_factor_name] = None
