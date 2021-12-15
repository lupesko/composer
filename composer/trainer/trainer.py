# Copyright 2021 MosaicML. All Rights Reserved.

from __future__ import annotations

import contextlib
import datetime
import itertools
import logging
import textwrap
import warnings
from typing import Any, Callable, ContextManager, Dict, List, Optional, Sequence, Tuple, Union, cast

import torch
import torch.distributed
import torch.utils.data
from torch.backends import cudnn
from torch.cuda.amp.grad_scaler import GradScaler
from torch.nn.parallel import DistributedDataParallel
from torchmetrics.collections import MetricCollection
from torchmetrics.metric import Metric

from composer.core import Callback, Engine, Event, Logger, State
from composer.core.algorithm import Algorithm
from composer.core.logging import BaseLoggerBackend, LogLevel
from composer.core.types import (Batch, BreakEpochException, DataLoader, Metrics, Optimizers, Precision, Schedulers,
                                 Tensor)
from composer.datasets import DataloaderSpec
from composer.datasets.dataloader import DDPDataLoader
from composer.loggers.tqdm_logger import TQDMLoggerBackend
from composer.models.base import BaseMosaicModel
from composer.optim import (ComposedScheduler, CosineAnnealingLRHparams, DecoupledSGDWHparams, OptimizerHparams,
                            SchedulerHparams, WarmUpLRHparams)
from composer.optim.scheduler import ensure_warmup_last
from composer.trainer.checkpoint import Checkpointer, CheckpointLoader
from composer.trainer.deepspeed import DeepSpeedHparams
from composer.trainer.devices.device import Device
from composer.trainer.devices.device_cpu import DeviceCPU
from composer.trainer.devices.device_gpu import DeviceGPU
from composer.trainer.scaler import ClosureGradScaler
from composer.trainer.trainer_hparams import TrainerHparams
from composer.utils import ddp, ensure_tuple, get_random_seed, map_collection, seed_all
from composer.utils.data import default_batch_split_fn

log = logging.getLogger(__name__)


class Trainer:
    """Trainer for training a model with algorithms.

    Can be created either with ``__init__`` or by providing a
    :class:`~composer.trainer.TrainerHparams` object
    (see :meth:`~composer.trainer.Trainer.create_from_hparams`).

    Args:
        model (BaseMosaicModel): The model to train.
        train_dataloader (DataLoader or DataloaderSpec): The dataloader or dataloader spec for the training data.
        eval_dataloader (DataLoader or DataloaderSpec): The dataloader or dataloader spec for the evaluation data.
        max_epochs (int): The maxmimum number of epochs to train for.
        algorithms (Sequence[Algorithm], optional): The algorithms to use during training.
            (default: ``[]``)
        optimizer_hparams: (OptimizerHparams | List[OptimizerHparams] | Tuple[OptimizerHparams, ...], optional):
            The OptimizerHparams for constructing
            the optimizer for training. Must pass OptimizerHparams instead of a `torch.optim.Optimizer`
            object because the optimizer has to be constructed after certain algorithms which modify
            the model architecture have run on the model. (default:
            ``MosaicMLSGDWHparams(lr=0.1, momentum=0.9, weight_decay=1.0e-4)``)
        schedulers_hparams: (SchedulerHparams | List[SchedulerHparams] | Tuple[SchedulerHparams, ...], optional): The
            SchedulerHparams for constructing the one or more learning rate schedulers used
            during training. Must pass SchedulerHparams instead of a `torch.optim.lr_scheduler._LRScheduler`
            object because the scheduler needs an optimizer to be constructed and we construct the optimizer
            in `__init__`. (default:
            ``[CosineAnnealingLRHparams(T_max=f"{max_epochs}ep"), WarmUpLRHparams()]``).
        device (Device, optional): The device to use for training. Either `DeviceCPU` or `DeviceGPU`.
            (default ``DeviceCPU(n_cpus=1)``)
        grad_accum (int, optional): The number of microbatches to split a per-device batch into. Gradients
            are summed over the microbatches per device. (default: ``1``)
        grad_clip_norm (float, optional): The norm to clip gradient magnitudes to. Set to None for no gradient
            clipping. (default: ``None``)
        validate_every_n_batches (int, optional): Compute metrics on evaluation data every N batches.
             Set to -1 to never validate on a batchwise frequency. (default: ``-1``)
        validate_every_n_epochs (int, optional): Compute metrics on evaluation data every N epochs.
            Set to -1 to never validate on a epochwise frequency. (default: ``1``)
        compute_training_metrics (bool, optional): True to compute metrics on training data and False to not.
            (default: ``False``)
        precision (Precision, optional): Numerical precision to use for training. (default: ``Precision.FP32``).
        ddp_sync_strategy (DDPSyncStrategy, optional): The strategy to use for synchronizing gradients.
            Leave unset to let the trainer auto-configure this.
        ddp_timeout (float, optional): Timeout, in seconds, for initializing the DDP process group.
            (default: ``5.0``)
        seed (int, optional): The seed used in randomization. When not provided a random seed
            will be created. (default: ``None``)
        deterministic_mode (bool, optional): Run the model deterministically. Experimental. Performance
            degradations expected. Certain Torch modules may not have deterministic implementations,
            which will result in a crash. (default: ``False``)
        log_destinations (List[BaseLoggerBackend], optional): The destinations to log training information to.
            (default ``[TQDMLoggerBackend()]``).
        callbacks (Sequence[Callback], optional): The callbacks to run during training. (default: ``[]``)
        checkpoint_filepath (str, optional): The path to a trainer checkpoint file. If provided
            the trainer will load the state (along with it's associated attributes) during initialization.
            (default: ``None``)
        checkpoint_folder (str, optional): The folder to save checkpoints to. Relative to the run directory, 
            (default: ``checkpoints``)
        checkpoint_interval (int, optional): The frequency with which to checkpoint. (default: ``1``)
        checkpoint_interval_unit (int, optional): Unit for the checkpoint save interval -- should be 'ep'
            for epochs, 'it' for iterations, or None to disable checkpointing. (default: ``None``).
        train_subset_num_batches (int, optional): If specified, finish every epoch early after training
            on this many batches. This parameter has no effect if it is greater than ``len(train_dataloader)``.
            If None (the default), then the entire dataloader will be iterated over.
        eval_subset_num_batches (int, optional): If specified, evaluate on this many batches.
            This parameter has no effect if it is greater than ``len(eval_dataloader)``.
            If None (the default), then the entire dataloader will be iterated over.
        deepspeed_hparams (DeepspeedHparams, optional): If specified, parameters to use for the
            deepseped engine.
        config (Dict[str, Any], optional): Extra user-provided trainer configuration. Will be persisted
            along with the trainer state during checkpointing. (default: ``None``)

    Attributes:
        state (State): The :class:`State` object used to store training state.
        logger (Logger): The :class:`Logger` used for logging.
        engine (Engine): The :class:`Engine` used for running callbacks and algorithms.
    """

    def __init__(
            self,
            *,
            model: BaseMosaicModel,
            train_dataloader: Union[DataLoader, DataloaderSpec],
            eval_dataloader: Union[DataLoader, DataloaderSpec],
            max_epochs: int,
            algorithms: Sequence[Algorithm] = tuple(),
            optimizer_hparams: Union[OptimizerHparams, Tuple[OptimizerHparams, ...], List[OptimizerHparams]] = tuple(),
            schedulers_hparams: Union[SchedulerHparams, Tuple[SchedulerHparams, ...], List[SchedulerHparams]] = tuple(),

            # device
            device: Optional[Device] = None,

            # training hparams
            grad_accum: int = 1,
            grad_clip_norm: Optional[float] = None,
            validate_every_n_batches: int = -1,
            validate_every_n_epochs: int = 1,
            compute_training_metrics: bool = False,
            precision: Precision = Precision.FP32,

            # ddp hparams
            ddp_sync_strategy: Optional[Union[str, ddp.DDPSyncStrategy]] = None,
            ddp_timeout: float = 5.0,

            # Randomness
            seed: Optional[int] = None,
            deterministic_mode: bool = False,

            # Logging and callbacks
            log_destinations: Optional[List[BaseLoggerBackend]] = None,
            callbacks: Sequence[Callback] = tuple(),

            # Checkpoint hparams
            checkpoint_filepath: Optional[str] = None,
            checkpoint_interval_unit: Optional[str] = None,
            checkpoint_folder: str = "checkpoints",
            checkpoint_interval: int = 1,

            # Subset parameters
            train_subset_num_batches: Optional[int] = None,
            eval_subset_num_batches: Optional[int] = None,

            # DeepSpeed
            deepspeed_hparams: Optional[DeepSpeedHparams] = None,

            # Optional config (ex. an hparams yaml file)
            config: Optional[Dict[str, Any]] = None):
        # surpressing GradScaler warnings as they are always created
        # self._use_grad_scaling() will raise a RuntimeError if grad scaling is not available when it is required
        warnings.filterwarnings(action="ignore", message="torch.cuda.amp.GradScaler")

        self.config = config

        self.deepspeed_enabled = deepspeed_hparams and deepspeed_hparams.enabled

        if not device:
            device = DeviceCPU() if not self.deepspeed_enabled else DeviceGPU()
        self._device = device

        if not seed:
            # Set a deterministic seed in the hparams
            # This seed will be dumped in the hparams that are saved with checkpoints
            seed = get_random_seed()
            log.info(f"Seed was None. Setting seed to random value: {seed}")
        # If hparams is used to create the Trainer this function is called twice
        # which is okay because all runs with the hparams codepath will do this
        seed_all(seed)
        self.seed = seed

        if self.deepspeed_enabled:
            import deepspeed
            deepspeed.init_distributed()
        else:
            ddp.initialize_ddp(device.ddp_backend, datetime.timedelta(seconds=ddp_timeout))

        if isinstance(train_dataloader, DataloaderSpec):
            self._train_device_transformation_fn = train_dataloader.device_transform_fn
            self._train_split_fn = train_dataloader.split_fn
            train_dataloader = train_dataloader.dataloader
        else:
            self._train_device_transformation_fn = None
            self._train_split_fn = None

        if isinstance(eval_dataloader, DataloaderSpec):
            eval_dataloader_spec = eval_dataloader
        else:
            eval_dataloader_spec = DataloaderSpec(eval_dataloader)
        self._eval_device_transformation_fn = eval_dataloader_spec.device_transform_fn
        self._eval_split_fn = eval_dataloader_spec.split_fn

        # TODO(#123): DeepSpeed still needs a precision context, but it's not completely clear how to
        # handle this with our version of Pytorch
        precision_context = self.device.precision_context if not self.deepspeed_enabled else cast(
            Callable[..., ContextManager], contextlib.nullcontext)

        self.state = State(
            max_epochs=max_epochs,
            algorithms=algorithms,
            callbacks=callbacks,
            model=model,
            grad_accum=grad_accum,
            precision=precision,
            precision_context=precision_context,
            train_dataloader=DDPDataLoader(train_dataloader),
            eval_dataloader=DDPDataLoader(eval_dataloader_spec.dataloader),
        )
        self.state.train_metrics = self._get_metrics_as_collection(is_train=True)

        # Steps per epoch
        self.state.steps_per_epoch = train_subset_num_batches

        if eval_subset_num_batches is not None:
            if eval_subset_num_batches > len(self.state.eval_dataloader):
                warnings.warn(
                    textwrap.dedent(f"""SubsetNumBatchesWarning: The eval_subset_num_batches({eval_subset_num_batches})
                        is greater than the number of batches in the evaluation dataloader
                        ({len(self.state.eval_dataloader)})"""))

        self._eval_subset_num_batches = eval_subset_num_batches

        if not log_destinations:
            log_destinations = [TQDMLoggerBackend()]
        self.logger = Logger(self.state, log_destinations)
        self.state.callbacks = [*log_destinations, *callbacks]
        self.engine = Engine(self.state, self.state.algorithms, self.logger, self.state.callbacks)

        self.validate_every_n_batches = validate_every_n_batches
        self.validate_every_n_epochs = validate_every_n_epochs
        self.compute_training_metrics = compute_training_metrics
        self._grad_clip_norm = grad_clip_norm

        if ddp_sync_strategy is None:
            self.ddp_sync_strategy = ddp.DDPSyncStrategy.SINGLE_AUTO_SYNC if not self.find_unused_parameters else ddp.DDPSyncStrategy.FORCED_SYNC
        else:
            self.ddp_sync_strategy = ddp.DDPSyncStrategy(ddp_sync_strategy)

        if deterministic_mode:
            torch.use_deterministic_algorithms(True)
            cudnn.benchmark = False
            warnings.warn("Deterministic mode is activated. This will negatively impact performance.",
                          category=UserWarning)

        # run INIT event before optimizers and schedulers are created
        self.engine.run_event(Event.INIT)

        # Need to use hparams here because optimizer and schedulers need to be created after Event.INIT
        if len(ensure_tuple(optimizer_hparams)) == 0:
            optimizer_hparams = DecoupledSGDWHparams(lr=0.1, momentum=0.9, weight_decay=1.0e-4)
        if len(ensure_tuple(schedulers_hparams)) == 0:
            schedulers_hparams = [CosineAnnealingLRHparams(T_max=f"{max_epochs}ep"), WarmUpLRHparams()]
        optimizers = [
            optimizer_hparams.initialize_object(param_group=self.state.model.parameters())
            for optimizer_hparams in ensure_tuple(optimizer_hparams)
        ]
        if len(optimizers) != 1:
            raise NotImplementedError("Multiple optimizers are not supported.")
        schedulers = [
            x.initialize_object(optimizers[0], self.state.steps_per_epoch)
            for x in ensure_warmup_last(list(ensure_tuple(schedulers_hparams)))
        ]
        self.state.optimizers = optimizers
        self.state.schedulers = [ComposedScheduler(schedulers=schedulers)]

        # TODO(#121): get checkpointing working with DeepSpeed.
        if checkpoint_interval_unit is not None and self.deepspeed_enabled:
            raise NotImplementedError("Checkpointing is not yet supported with DeepSpeed.")
        self._checkpointer = Checkpointer(checkpoint_folder=checkpoint_folder,
                                          checkpoint_interval=checkpoint_interval,
                                          checkpoint_interval_unit=checkpoint_interval_unit)

        self.checkpoint_loader = None
        # TODO(#121): get checkpointing working with DeepSpeed.
        if checkpoint_filepath:
            if self.deepspeed_enabled:
                raise NotImplementedError("Checkpointing is not yet supported with DeepSpeed.")
            self.checkpoint_loader = CheckpointLoader(checkpoint_filepath=checkpoint_filepath)
            self.checkpoint_loader.load_checkpoint(state=self.state)

        # place the state, model in the proper devices
        if self.deepspeed_enabled:
            import deepspeed

            optimizer = self.state.optimizers[0]

            deepspeed_config: dict[str, Any] = {
                "train_batch_size": self.state.train_batch_size,
                "gradient_accumulation_steps": self.state.grad_accum,
            }

            if self.state.precision == Precision.AMP:
                deepspeed_config["amp"] = {"enabled": True}
            elif self.state.precision == Precision.FP16:
                deepspeed_config["fp16"] = {"enabled": True}

            if self.grad_clip_norm:
                deepspeed_config["gradient_clipping"] = self.grad_clip_norm

            (self.state.model, self.state.optimizers, _, _) = deepspeed.initialize(
                config=deepspeed_config,
                model=self.state.model,
                optimizer=optimizer,
            )
        else:
            self.state.model = self.device.module_to_device(self.state.model)
            self.state.optimizers = map_collection(self.state.optimizers, self.device.optimizer_to_device)

            # wrap model with DDP
            self.state.model = ddp.prepare_module(self.state.model, self.find_unused_parameters)

        # print training start
        self.logger.metric_fit({"trainer/algorithms": [str(algo) for algo in self.engine.algorithms]})

        if self.compute_training_metrics:
            warnings.warn(
                textwrap.dedent("""Computing model evaluation metrics during training.
                    This doubles the number of forward passes and may lead
                    to a throughput degradation."""))

        if self._use_closures():

            def _ddp_reduce_scalar_and(flag: bool) -> bool:
                value = 1 if flag else 0
                flag_tensor = self.device.tensor_to_device(torch.tensor(value).int())
                ddp.all_reduce(flag_tensor, reduce_operation='PRODUCT')
                return flag_tensor.item() == 1

            def _ddp_reduce_tensor_sum(tensor: Tensor) -> Tensor:
                # Happens in-place; that's fine
                ddp.all_reduce(tensor, reduce_operation="SUM")
                return tensor

            self.state.scaler = ClosureGradScaler(ddp_reduce_scalar_and=_ddp_reduce_scalar_and,
                                                  ddp_reduce_tensor_sum=_ddp_reduce_tensor_sum)
        else:
            self.state.scaler = GradScaler()

        self.engine.run_event(Event.TRAINING_START)

        self._spin_dataloaders()

        if self.state.batch_idx == 0 and self.checkpoint_loader is not None:
            # only restore the rng state here if the step in the current epoch is zero.
            self.checkpoint_loader.restore_checkpoint_rng_state(self.state, self.device)
            self.checkpoint_loader = None

        self._train_dataloader_iterator = None

    @classmethod
    def create_from_hparams(cls, hparams: TrainerHparams) -> Trainer:
        """Instantiate a Trainer using a `TrainerHparams` object.

        Args:
            hparams (TrainerHparams): The TrainerHparams object used to instantiate the trainer.

        Returns:
            A Trainer object initialized with the provided TrainerHparams.
        """

        hparams.validate()

        # devices and systems
        device = hparams.device.initialize_object()

        seed = hparams.seed if hparams.seed else get_random_seed()
        # need to set seed before model initialization for determinism
        seed_all(seed)

        model = hparams.model.initialize_object()
        algorithms = [x.initialize_object() for x in hparams.algorithms]

        # callbacks, loggers, and seed
        callbacks = [x.initialize_object() for x in hparams.callbacks]
        dict_config = hparams.to_dict()
        log_destinations = [x.initialize_object(config=dict_config) for x in hparams.loggers]

        train_device_batch_size = hparams.train_batch_size // ddp.get_world_size()
        if hparams.train_dataset.shuffle and hparams.train_subset_num_batches:
            warnings.warn(
                textwrap.dedent(f"""SubsetNumBatchesWarning: When specifying train_subset_num_batches,
            (set to {hparams.train_subset_num_batches}), train_datset.shuffle should be set to False. Otherwise,
            each training epoch may load a different subset of samples."""))
        train_dataloader = hparams.train_dataset.initialize_object(train_device_batch_size, hparams.dataloader)

        eval_device_batch_size = hparams.eval_batch_size // ddp.get_world_size()
        if hparams.val_dataset.shuffle and hparams.eval_subset_num_batches:
            warnings.warn(
                textwrap.dedent(f"""SubsetNumBatchesWarning: When specifying eval_subset_num_batches,
            (set to {hparams.eval_subset_num_batches}), val_dataset.shuffle should be set to False. Otherwise,
            each evaluation epoch may load a different subset of samples."""))
        eval_dataloader = hparams.val_dataset.initialize_object(eval_device_batch_size, hparams.dataloader)

        trainer = cls(
            model=model,
            train_dataloader=train_dataloader,
            eval_dataloader=eval_dataloader,
            max_epochs=hparams.max_epochs,
            algorithms=algorithms,
            optimizer_hparams=hparams.optimizer,
            schedulers_hparams=hparams.schedulers,

            # device
            device=device,

            # training hparams
            grad_accum=hparams.grad_accum,
            grad_clip_norm=hparams.grad_clip_norm,
            validate_every_n_batches=hparams.validate_every_n_batches,
            validate_every_n_epochs=hparams.validate_every_n_epochs,
            compute_training_metrics=hparams.compute_training_metrics,
            precision=hparams.precision,

            # ddp hparams
            ddp_sync_strategy=hparams.ddp_sync_strategy,
            ddp_timeout=hparams.ddp_timeout,

            # Randomness
            seed=seed,
            deterministic_mode=hparams.deterministic_mode,

            # Callbacks and logging
            log_destinations=log_destinations,
            callbacks=tuple(callbacks),

            # Checkpointing hparams
            checkpoint_filepath=hparams.checkpoint_filepath,
            checkpoint_interval_unit=hparams.checkpoint_interval_unit,
            checkpoint_folder=hparams.checkpoint_folder,
            checkpoint_interval=hparams.checkpoint_interval,

            # Subset parameters
            train_subset_num_batches=hparams.train_subset_num_batches,
            eval_subset_num_batches=hparams.eval_subset_num_batches,

            # DeepSpeed
            deepspeed_hparams=hparams.deepspeed,

            # Optional config
            config=hparams.to_dict())

        return trainer

    @property
    def model(self) -> BaseMosaicModel:
        """The original model"""
        return ddp.get_original_model(self.state.model)

    @property
    def train_dataloader(self) -> Union[DataLoader, DataloaderSpec]:
        """The train dataloader"""
        if self._train_split_fn is not None and self._train_device_transformation_fn is not None:
            return DataloaderSpec(self.state.train_dataloader, self._train_device_transformation_fn,
                                  self._train_split_fn)
        else:
            return self.state.train_dataloader

    @train_dataloader.setter
    def train_dataloader(self, train_dataloader: Union[DataLoader, DataloaderSpec]):
        if isinstance(train_dataloader, DataloaderSpec):
            self._train_device_transformation_fn = train_dataloader.device_transform_fn
            self._train_split_fn = train_dataloader.split_fn
            dataloader = train_dataloader.dataloader
        else:
            self._train_device_transformation_fn = None
            self._train_split_fn = None
            dataloader = train_dataloader
        if not isinstance(dataloader, DDPDataLoader):
            dataloader = DDPDataLoader(dataloader)
        self.state.train_dataloader = dataloader

    # TODO(anis) -- add getters/setters for evaluators

    @property
    def max_epochs(self):
        """Maximum number of training epochs"""
        return self.state.max_epochs

    @max_epochs.setter
    def max_epochs(self, max_epochs: int):
        self.state.max_epochs = max_epochs

    @property
    def algorithms(self):
        """Algorithms"""
        return self.state.algorithms

    @algorithms.setter
    def algorithms(self, algorithms: Sequence[Algorithm]):
        self.state.algorithms = algorithms

    @property
    def callbacks(self):
        """Callbacks"""
        return self.state.callbacks

    @callbacks.setter
    def callbacks(self, callbacks: Sequence[Callback]):
        # Preserve the logger backends as callbacks
        self.state.callbacks = [*self.logger.backends, *callbacks]

    @property
    def optimizers(self):
        """Optimizers"""
        return self.state.optimizers

    @optimizers.setter
    def optimizers(self, optimizers: Optimizers):
        self.state.optimizers = optimizers

    @property
    def schedulers(self):
        """Schedulers"""
        return self.state.schedulers

    @schedulers.setter
    def schedulers(self, schedulers: Schedulers):
        self.state.schedulers = schedulers

    @property
    def device(self):
        """Device"""
        # No setter for device since it wouldn't make sense to change it
        return self._device

    @property
    def grad_accum(self):
        """Gradient Accumulation"""
        return self.state.grad_accum

    @grad_accum.setter
    def grad_accum(self, grad_accum: int):
        self.state.grad_accum = grad_accum

    @property
    def grad_clip_norm(self):
        """Gradient Clipping Norm"""
        return self._grad_clip_norm

    @grad_clip_norm.setter
    def grad_clip_norm(self, grad_clip_norm: Optional[float]):
        if self.deepspeed_enabled:
            raise RuntimeError("Unable to update the grad_clip_norm if using deepspeed")
        self._grad_clip_norm = grad_clip_norm

    @property
    def precision(self):
        return self.state.precision

    @precision.setter
    def precision(self, precision: Union[str, Precision]):
        self.state.precision = precision

    @property
    def log_destinations(self):
        return self.logger.backends

    @log_destinations.setter
    def log_destinations(self, log_destinations: Sequence[BaseLoggerBackend]):
        # first, remove existing log destinations as callbacks
        for log_destination in self.logger.backends:
            self.state.callbacks.remove(log_destination)

        # Update the backends
        self.logger.backends[:] = log_destinations

        # Update the callbacks
        self.state.callbacks = [*log_destinations, *self.state.callbacks]

    @property
    def checkpoint_folder(self):
        return self._checkpointer.checkpoint_folder

    @checkpoint_folder.setter
    def checkpoint_folder(self, checkpoint_folder: str):
        self._checkpointer.checkpoint_folder = checkpoint_folder

    @property
    def checkpoint_interval(self):
        return self._checkpointer.save_interval

    @checkpoint_interval.setter
    def checkpoint_interval(self, checkpoint_interval: int):
        self._checkpointer.save_interval = checkpoint_interval

    @property
    def checkpoint_interval_unit(self):
        return self._checkpointer.checkpoint_interval_unit

    @checkpoint_interval_unit.setter
    def checkpoint_interval_unit(self, checkpoint_interval_unit: Optional[str]):
        self._checkpointer.checkpoint_interval_unit = checkpoint_interval_unit

    @property
    def train_subset_num_batches(self):
        return self.state._steps_per_epoch

    @train_subset_num_batches.setter
    def train_subset_num_batches(self, train_subset_num_batches: Optional[int] = None):
        self.state.steps_per_epoch = train_subset_num_batches

    @property
    def eval_subset_num_batches(self):
        return self._eval_subset_num_batches

    @eval_subset_num_batches.setter
    def eval_subset_num_batches(self, eval_subset_num_batches: Optional[int] = None):
        self._eval_subset_num_batches = eval_subset_num_batches

    def fit(self, num_batches: Optional[int] = None, num_epochs: Optional[int] = None):
        """Train and evaluate the model on the provided data.

        By default, it trains until the exit condition speicifed by ``max_epochs``.
        You can optionally specify one of ``num_batches`` or ``num_epochs`` to train 
        for the specified duration. This will continue the training process from the last
        call to :meth:`fit`.
        
        Args:
            num_batches (int, optional): Train for the specified number of batches. Cannot be
                specified with ``num_epochs``.
            num_epochs (int, optional): Train for the specified number of epochs. Cannot be
                speciifed with ``num_batches``.
        """

        # shorthand
        state = self.state

        if self.engine.closed:
            raise RuntimeError(
                textwrap.dedent("""Cannot .fit() if the engine is already closed.
                This would occur if the trainer already finished training to max_epochs, or if
                an exception occured during the training process. Please create a new trainer to train."""))

        if num_batches is not None and num_epochs is not None:
            raise ValueError("Only one of num_batches or num_epochs can be provided")
        if self.state.batch_idx != 0 and num_epochs is not None:
            batches_remaining_in_epoch = state.steps_per_epoch - state.batch_idx
            raise ValueError(
                textwrap.dedent(f"""Num_epochs cannot be specified when the trainer is mid-epoch.
                Instead, call trainer.fit(num_batches={batches_remaining_in_epoch}), which will advance the
                trainer to the end of the current epoch"""))
        max_step = float('inf')
        max_epoch = state.max_epochs
        if num_batches is not None:
            max_step = state.step + num_batches
        if num_epochs is not None:
            max_epoch = state.epoch + num_epochs
        try:
            while state.epoch < max_epoch and state.step < max_step:
                if state.batch_idx == 0:
                    self.engine.run_event(Event.EPOCH_START)
                    self.logger.metric_epoch({"epoch": self.state.epoch})

                    self._train_dataloader_iterator = iter(
                        itertools.islice(self.state.train_dataloader, self.state.steps_per_epoch))
                else:
                    # if resuming from checkpoint, then spin the dataloader back to where we were:
                    if self.checkpoint_loader is not None:
                        # resuming a checkpoint mid epoch. Checkpoint loader to None after
                        assert self._train_dataloader_iterator is None, "If resuming from a checkpoint mid epoch, iterator should be None"
                        self._train_dataloader_iterator = iter(
                            itertools.islice(state.train_dataloader, state.steps_per_epoch))
                        for _ in range(self.state.batch_idx):
                            next(self._train_dataloader_iterator)
                        self.checkpoint_loader.restore_checkpoint_rng_state(self.state, self.device)
                        self.checkpoint_loader = None

                assert self._train_dataloader_iterator is not None, "iterator is set on self._epoch_start() or via the checkpoint loader"

                while state.step < max_step:
                    try:
                        state.batch = next(self._train_dataloader_iterator)
                    except StopIteration:
                        break

                    try:
                        self._train_batch()
                    except BreakEpochException:
                        log.info(f'Skipping the `rest of Epoch {state.epoch}')
                        state.step += state.steps_per_epoch - state.batch_idx

                if state.batch_idx == state.steps_per_epoch:
                    self._epoch_end()

            self.engine.run_event(Event.TRAINING_END)
        except:
            # TODO allow subsequent calls to fit even if an exception occurs
            self.engine.close()
            raise
        else:
            if state.epoch == state.max_epochs:
                self.engine.close()

    @property
    def backwards_create_graph(self):
        return any(map(lambda x: x.backwards_create_graph, self.state.algorithms))

    @property
    def find_unused_parameters(self):
        return any(map(lambda x: x.find_unused_parameters, self.state.algorithms))

    def _get_metrics_as_collection(self, *, is_train: bool) -> MetricCollection:
        """Get metrics relevant to the model. Metrics are all implemented as subclasses
        of :class:`torchmetrics.Metric`. This function returns metrics as a
        :class:`~torchmetrics.collections.MetricCollection` to enable support
        for multiple metrics.

        Args:
            is_train (bool): True to get training metrics and false to get
            evaluation metrics.

        Returns:
            A :class:`~torchmetrics.collections.MetricCollection` object.
        """
        metrics = ddp.get_original_model(self.state.model).metrics(train=is_train)
        assert isinstance(metrics, (Metric, MetricCollection)), \
            "Error module.metrics() must return a Metric or MetricCollection object."
        if isinstance(metrics, Metric):
            # Forcing metrics to be a MetricCollection simplifies logging results
            metrics = MetricCollection([metrics])

        # Safety check to ensure the metric and data are on the same device. Normally not
        # needed because the metric is automatically on the same device as the model.
        # See https://torchmetrics.readthedocs.io/en/latest/pages/overview.html for details.
        metrics = self.device.module_to_device(metrics)

        # HACK: DeepSpeed somehow manages to convert metric internal states to its own dtype. When
        # running with FP16, this tends to result in overflows. Let's assume FP32 is good enough.
        for _, metric in metrics.items():
            metric.set_dtype(torch.float32)  # type: ignore

        return metrics

    def _compute_and_log_metrics(self, metrics: Metrics, *, is_train: bool, is_batch: bool):
        """Computes metrics, logs the results, and resets the metrics.

        Args:
            metrics (Metrics): The metrics to compute.
            is_train (bool): True for training metrics, False for evaluation metrics.
            is_batch (bool): True if logging at batch level, false for epoch level.
        """
        computed_metrics = metrics.compute()
        for name, value in computed_metrics.items():
            log_level = LogLevel.BATCH if is_batch else LogLevel.EPOCH
            suffix = 'train' if is_train else 'val'
            self.logger.metric(log_level, {f'{name.lower()}/{suffix}': value})
        metrics.reset()

    def _spin_dataloaders(self):
        """Spin the dataloaders to restore sampler state.

        Only one batch must be loaded to seed the sampler's generator.
        since only the first batch is being loaded, the dataloader may
        not be completely iterated through.
        """
        # surpressing this multiple iteration warning -- it is OK to ignore
        warnings.filterwarnings(action="ignore", message=r"^DataloaderMultipleIterationWarning", append=True)

        # spin the eval dataloader once to initialize its sampler deterministically
        # so it does not affect any other RNG reads
        for _ in self.state.eval_dataloader:
            break

        # spin the train dataloader's sampler to get to the state of the desired epoch
        for _ in range(self.state.epoch):
            for _ in self.state.train_dataloader:
                break

    def _get_batch_size(self, batch: Batch) -> int:
        if isinstance(batch, Tensor):
            return batch.shape[0]

        dim0_sizes = []
        if isinstance(batch, (list, tuple)):
            for tensors in batch:
                for t in ensure_tuple(tensors):
                    dim0_sizes.append(t.shape[0])
        elif isinstance(batch, dict):
            dim0_sizes = [t.shape[0] for t in batch.values()]

        if len(set(dim0_sizes)) == 1:
            return dim0_sizes[0]
        else:
            raise ValueError('The default _get_batch_size function found ',
                             f'multiple Tensor sizes in batch: {dim0_sizes}')

    def _epoch_end(self):
        # shorthand
        state = self.state

        for scheduler in state.schedulers:
            scheduler.step(interval='epoch')  # type: ignore

        self.engine.run_event(Event.EPOCH_END)

        if self.validate_every_n_epochs > 0 and (state.epoch + 1) % self.validate_every_n_epochs == 0:
            self.eval(is_batch=False)

        state.epoch += 1

        if self._checkpointer.should_checkpoint(state=state, event=Event.EPOCH_END):
            self._checkpointer.save_checkpoint(state=state, seed=self.seed, device=self.device, config=self.config)

        self._train_dataloader_iterator = None

    def _train_batch(self):
        """Helper method to train a batch. Assumes that state.batch is set to the batch to be trained.
        """
        state = self.state
        assert state.scaler is not None, "state.scalar should be set in __init__"
        assert state.train_metrics is not None, "state.train_metrics should be set via .fit()"

        state.last_batch_size = self._get_batch_size(state.batch)
        state.batch = self.device.batch_to_device(state.batch)
        if self._train_device_transformation_fn is not None:
            state.batch = self._train_device_transformation_fn(state.batch)

        if self._train_split_fn is None:
            split_fn = default_batch_split_fn
        else:
            split_fn = self._train_split_fn

        if self.compute_training_metrics:
            # compute metrics on the training set
            state.model.eval()
            with torch.no_grad():
                eval_microbatches = split_fn(state.batch, state.grad_accum)
                for eval_microbatch in eval_microbatches:
                    # TODO: Detect if self.run_event(Event.AFTER_DATALOADER) changes the training
                    # data and if so print a warning that metrics may return unexpected results
                    outputs, targets = ddp.get_original_model(state.model).validate(eval_microbatch)
                    state.train_metrics.update(outputs, targets)

        state.model.train()

        self.engine.run_event(Event.AFTER_DATALOADER)

        microbatches = split_fn(state.batch, state.grad_accum)

        self.engine.run_event(Event.BATCH_START)
        use_grad_scaling = self._use_grad_scaling(state.precision, state.scaler)
        self.logger.metric_batch({
            "trainer/global_step": self.state.step,
            "trainer/batch_idx": self.state.batch_idx,
        })
        total_loss = None
        if self.deepspeed_enabled:
            total_loss = self._train_batch_inner(microbatches)
        elif self._use_closures():
            closure = lambda **kwargs: self._train_batch_inner(microbatches, **kwargs)
            for optimizer in state.optimizers:
                if use_grad_scaling:
                    total_loss = state.scaler.step(optimizer, closure=closure)
                else:
                    # Torch optimizers technically expect closures to return a float, not a Tensor.
                    # In practice, this doesn't seem to actually matter.
                    total_loss = optimizer.step(closure=closure)  # type: ignore
        else:
            total_loss = self._train_batch_inner(microbatches)
            for optimizer in state.optimizers:
                if use_grad_scaling:
                    state.scaler.step(optimizer)
                else:
                    optimizer.step()

        if use_grad_scaling:
            state.scaler.update()

        if total_loss is not None:
            assert isinstance(total_loss, Tensor)

            # total_loss can be None if gradient scaling failed
            ddp.all_reduce(total_loss, reduce_operation="SUM")
            ddp.barrier()
            full_loss = total_loss.cpu().item()
            self.logger.metric_batch({'loss/train': full_loss / ddp.get_world_size()})

        if self.compute_training_metrics:
            self._compute_and_log_metrics(state.train_metrics, is_train=True, is_batch=True)

        self.engine.run_event(Event.BATCH_END)

        for scheduler in state.schedulers:
            scheduler.step(interval='batch')  # type: ignore

        if self.validate_every_n_batches > 0 and (state.step + 1) % self.validate_every_n_batches == 0:
            self.eval(is_batch=True)

        state.step += 1
        if self._checkpointer.should_checkpoint(state=state, event=Event.BATCH_END):
            self._checkpointer.save_checkpoint(state=state, seed=self.seed, device=self.device, config=self.config)

    def _train_batch_inner(self, microbatches: Sequence[Batch], ddp_sync: bool = True):
        """Run training on a full batch of data.

        Args:
            microbatches (Sequence[Batch]): The microbatches which make up the batch.
            ddp_sync (bool): True to sync gradients between devices on every backwards
                pass and False to only sync gradients after each device has finished
                computing a gradient on it's entire set of microbatches. (default: ``True``)
        Returns:
            float: Total loss
        """
        if ddp_sync or not isinstance(self.state.model, DistributedDataParallel):
            context = contextlib.nullcontext
        else:
            context = self.state.model.no_sync

        with context():  # type: ignore - Pyright apparently doesn't recognize no_sync
            self.engine.run_event(Event.BEFORE_TRAIN_BATCH)

            state = self.state
            assert state.optimizers is not None
            assert state.scaler is not None

            use_grad_scaling = self._use_grad_scaling(state.precision, state.scaler)

            if not self.deepspeed_enabled:
                for optimizer in state.optimizers:
                    optimizer.zero_grad()

            # tracker for gradient accumulation
            total_loss = self.device.tensor_to_device(torch.zeros(size=(1,)))
            current_batch_size = sum([self._get_batch_size(batch) for batch in microbatches])

            for microbatch_idx, state.batch in enumerate(microbatches):
                is_final_microbatch = microbatch_idx + 1 == len(microbatches)
                sync_context = contextlib.nullcontext() if self.deepspeed_enabled else ddp.sync_context(
                    state, is_final_microbatch, self.ddp_sync_strategy)
                with sync_context:
                    last_microbatch_size = self._get_batch_size(state.batch)

                    # forward pass
                    self.engine.run_event(Event.BEFORE_FORWARD)

                    with state.precision_context:
                        state.outputs = state.model.forward(state.batch)

                    self.engine.run_event(Event.AFTER_FORWARD)

                    # loss
                    self.engine.run_event(Event.BEFORE_LOSS)

                    with state.precision_context:
                        state.loss = ddp.get_original_model(state.model).loss(state.outputs, state.batch)

                    # We always want to scale loss by the grad_accum before the backwards pass and
                    # also for sake of metrics. Complicating matters, the DeepSpeed engine does its
                    # own scaling when we call `.backward`, but this isn't in place so we still need
                    # to scale for sake of metrics after the `.backward` call.

                    # Loss is added to losses with clone to not scale the loss for the step printout
                    # Likely need to look into the performance impact
                    if not self.deepspeed_enabled:
                        for loss in ensure_tuple(state.loss):
                            loss.mul_(last_microbatch_size / current_batch_size)
                            total_loss += loss.detach().clone()

                    assert state.loss is not None
                    self.engine.run_event(Event.AFTER_LOSS)

                    # backward
                    self.engine.run_event(Event.BEFORE_BACKWARD)

                    if use_grad_scaling:
                        state.loss = state.scaler.scale(state.loss)

                    if self.deepspeed_enabled:
                        state.model.backward(state.loss)  # type: ignore

                        # This is the same loss scaling and reporting we skipped earlier.
                        for loss in ensure_tuple(state.loss):
                            loss.mul_(last_microbatch_size / current_batch_size)
                            total_loss += loss.detach().clone()
                    else:
                        for loss in ensure_tuple(state.loss):
                            loss.backward(create_graph=self.backwards_create_graph)

                    self.engine.run_event(Event.AFTER_BACKWARD)

                if self.deepspeed_enabled:
                    state.model.step()  # type: ignore

            # Unscale gradients before `Event.AFTER_TRAIN_BATCH`
            if use_grad_scaling:
                for optimizer in ensure_tuple(state.optimizers):
                    state.scaler.unscale_(optimizer)

            # clip gradients if the magnitude is too large
            if not self.deepspeed_enabled and self.grad_clip_norm is not None:
                torch.nn.utils.clip_grad_norm_(
                    parameters=state.model.parameters(),
                    max_norm=self.grad_clip_norm,
                )

            self.engine.run_event(Event.AFTER_TRAIN_BATCH)

            return total_loss

    def eval(self, is_batch: bool):
        """Evaluate the model on the provided evaluation data and log
        appropriate metrics.

        Args:
            is_batch (bool): True to log metrics with ``LogLevel.BATCH``
                and False to log metrics with ``LogLevel.EPOCH``.
        """
        state = self.state
        model = state.model

        restore_model_train = model.training

        model.eval()
        with torch.no_grad():

            self.engine.run_event(Event.EVAL_START)

            metrics = self._get_metrics_as_collection(is_train=False)

            for i, state.batch in enumerate(itertools.islice(state.eval_dataloader, self._eval_subset_num_batches)):
                state.batch = self.device.batch_to_device(state.batch)
                state.last_batch_size = self._get_batch_size(state.batch)
                if self._eval_device_transformation_fn is not None:
                    state.batch = self._eval_device_transformation_fn(state.batch)

                self.engine.run_event(Event.EVAL_BATCH_START)

                self.engine.run_event(Event.EVAL_BEFORE_FORWARD)
                state.outputs, targets = ddp.get_original_model(state.model).validate(state.batch)
                self.engine.run_event(Event.EVAL_AFTER_FORWARD)

                metrics.update(state.outputs, targets)

                self.engine.run_event(Event.EVAL_BATCH_END)

            self._compute_and_log_metrics(metrics, is_train=False, is_batch=is_batch)
            self.engine.run_event(Event.EVAL_END)

        if restore_model_train:
            model.train()

    def _use_grad_scaling(self, precision: Union[str, Precision], scaler: Optional[GradScaler]) -> bool:
        """Determines based on precision when to use grad scaling.

        By default, the pytorch GradScaler is a no-op if running on
        unsupported hardware. Here we raise a RuntimeError instead.

        Args:
            precision (Precision): Numerical precision, based on the Precision Enum.
            scaler (GradScaler): Used to make sure that the scaler is enabled when
            using grad scaling.

        Raises:
            RuntimeError:
                Occurs when attempting to use grad scaling without the scaler
                enabled. Likely due to hardware not supporting the provided precision.
        """
        if self.deepspeed_enabled:
            return False

        precision = Precision(precision)
        use_grad_scaling = precision == Precision.AMP

        if use_grad_scaling and (scaler is None or not scaler.is_enabled()):
            raise RuntimeError(f'Attempting to use grad scaling with {precision}, but scaler is not enabled.'
                               f'Potentially your hardware does not support Precision {precision}.')
        return use_grad_scaling

    def _use_closures(self) -> bool:
        """Determines based on precision and optimizers whether to use closures.

        We default to using closures unless AMP is enabled, in which case we only allow
        closures when using optimizers with the _step_supports_amp_closure flag.
        """
        if self.deepspeed_enabled:
            return False

        if self.state.precision != Precision.AMP:
            return True

        if self.state.optimizers is None:
            raise RuntimeError("state.optimizers must be set before `_use_closures` can be determined")

        return all(
            getattr(optimizer, "_step_supports_amp_closure", False)
            for optimizer in ensure_tuple(self.state.optimizers))
