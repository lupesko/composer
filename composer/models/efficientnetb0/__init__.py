# Copyright 2021 MosaicML. All Rights Reserved.

"""See the :doc:`Method Card</model_cards/efficientnet>` for more details."""
from composer.models.efficientnetb0.efficientnetb0_hparams import EfficientNetB0Hparams as EfficientNetB0Hparams
from composer.models.efficientnetb0.model import EfficientNetB0 as EfficientNetB0

__all__ = [EfficientNetB0Hparams, EfficientNetB0]

_task = 'Image Classification'
_dataset = 'ImageNet'
_name = 'EfficientNet-B0'
_quality = '76.63'
_metric = 'Top-1 Accuracy'
_ttt = '21h 48m'
_hparams = 'efficientnetb0.yaml'
