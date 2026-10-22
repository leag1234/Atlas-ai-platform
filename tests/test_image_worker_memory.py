"""Keep the real image pipeline loadable on both supported memory tiers."""

import unittest
from unittest.mock import Mock

from image_worker import place_pipeline


class ImageWorkerMemoryTests(unittest.TestCase):
    def test_small_gpu_offloads_before_any_cuda_placement(self) -> None:
        pipeline = Mock()
        place_pipeline(pipeline, 22 * 1024**3)
        pipeline.enable_sequential_cpu_offload.assert_called_once_with()
        pipeline.to.assert_not_called()

    def test_large_gpu_keeps_resident_pipeline(self) -> None:
        pipeline = Mock()
        place_pipeline(pipeline, 48 * 1024**3)
        pipeline.to.assert_called_once_with("cuda")
        pipeline.enable_sequential_cpu_offload.assert_not_called()
