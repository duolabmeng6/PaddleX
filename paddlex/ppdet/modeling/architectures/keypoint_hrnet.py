# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import paddle
import numpy as np
import math
from paddlex.ppdet.core.workspace import register, create
from .meta_arch import BaseArch
from ..keypoint_utils import transform_preds
from .. import layers as L

__all__ = ['TopDownHRNet']


@register
class TopDownHRNet(BaseArch):
    __category__ = 'architecture'
    __inject__ = ['loss']

    def __init__(self,
                 width,
                 num_joints,
                 backbone='HRNet',
                 loss='KeyPointMSELoss',
                 post_process='HRNetPostProcess',
                 flip_perm=None,
                 flip=True,
                 shift_heatmap=True):
        """
        HRNnet network, see https://arxiv.org/abs/1902.09212

        Args:
            backbone (nn.Layer): backbone instance
            post_process (object): `HRNetPostProcess` instance
            flip_perm (list): The left-right joints exchange order list
        """
        super(TopDownHRNet, self).__init__()
        self.backbone = backbone
        self.post_process = HRNetPostProcess()
        self.loss = loss
        self.flip_perm = flip_perm
        self.flip = flip
        self.final_conv = L.Conv2d(width, num_joints, 1, 1, 0, bias=True)
        self.shift_heatmap = shift_heatmap
        self.deploy = False

    @classmethod
    def from_config(cls, cfg, *args, **kwargs):
        # backbone
        backbone = create(cfg['backbone'])

        return {'backbone': backbone, }

    def _forward(self):
        feats = self.backbone(self.inputs)
        hrnet_outputs = self.final_conv(feats[0])

        if self.training:
            return self.loss(hrnet_outputs, self.inputs)
        elif self.deploy:
            return hrnet_outputs
        else:
            if self.flip:
                self.inputs['image'] = self.inputs['image'].flip([3])
                feats = self.backbone(self.inputs)
                output_flipped = self.final_conv(feats[0])
                output_flipped = self.flip_back(output_flipped.numpy(),
                                                self.flip_perm)
                output_flipped = paddle.to_tensor(output_flipped.copy())
                if self.shift_heatmap:
                    output_flipped[:, :, :, 1:] = output_flipped.clone(
                    )[:, :, :, 0:-1]
                hrnet_outputs = (hrnet_outputs + output_flipped) * 0.5
            imshape = (self.inputs['im_shape'].numpy()
                       )[:, ::-1] if 'im_shape' in self.inputs else None
            center = self.inputs['center'].numpy(
            ) if 'center' in self.inputs else np.round(imshape / 2.)
            scale = self.inputs['scale'].numpy(
            ) if 'scale' in self.inputs else imshape / 200.
            outputs = self.post_process(hrnet_outputs, center, scale)
            return outputs

    def get_loss(self):
        return self._forward()

    def get_pred(self):
        res_lst = self._forward()
        outputs = {'keypoint': res_lst}
        return outputs

    def flip_back(self, output_flipped, matched_parts):
        assert output_flipped.ndim == 4,\
                'output_flipped should be [batch_size, num_joints, height, width]'

        output_flipped = output_flipped[:, :, :, ::-1]

        for pair in matched_parts:
            tmp = output_flipped[:, pair[0], :, :].copy()
            output_flipped[:, pair[0], :, :] = output_flipped[:, pair[1], :, :]
            output_flipped[:, pair[1], :, :] = tmp

        return output_flipped


class HRNetPostProcess(object):
    def get_max_preds(self, heatmaps):
        '''get predictions from score maps

        Args:
            heatmaps: numpy.ndarray([batch_size, num_joints, height, width])

        Returns:
            preds: numpy.ndarray([batch_size, num_joints, 2]), keypoints coords
            maxvals: numpy.ndarray([batch_size, num_joints, 2]), the maximum confidence of the keypoints
        '''
        assert isinstance(heatmaps,
                          np.ndarray), 'heatmaps should be numpy.ndarray'
        assert heatmaps.ndim == 4, 'batch_images should be 4-ndim'

        batch_size = heatmaps.shape[0]
        num_joints = heatmaps.shape[1]
        width = heatmaps.shape[3]
        heatmaps_reshaped = heatmaps.reshape((batch_size, num_joints, -1))
        idx = np.argmax(heatmaps_reshaped, 2)
        maxvals = np.amax(heatmaps_reshaped, 2)

        maxvals = maxvals.reshape((batch_size, num_joints, 1))
        idx = idx.reshape((batch_size, num_joints, 1))

        preds = np.tile(idx, (1, 1, 2)).astype(np.float32)

        preds[:, :, 0] = (preds[:, :, 0]) % width
        preds[:, :, 1] = np.floor((preds[:, :, 1]) / width)

        pred_mask = np.tile(np.greater(maxvals, 0.0), (1, 1, 2))
        pred_mask = pred_mask.astype(np.float32)

        preds *= pred_mask

        return preds, maxvals

    def get_final_preds(self, heatmaps, center, scale):
        """the highest heatvalue location with a quarter offset in the
        direction from the highest response to the second highest response.

        Args:
            heatmaps (numpy.ndarray): The predicted heatmaps
            center (numpy.ndarray): The boxes center
            scale (numpy.ndarray): The scale factor

        Returns:
            preds: numpy.ndarray([batch_size, num_joints, 2]), keypoints coords
            maxvals: numpy.ndarray([batch_size, num_joints, 1]), the maximum confidence of the keypoints
        """

        coords, maxvals = self.get_max_preds(heatmaps)

        heatmap_height = heatmaps.shape[2]
        heatmap_width = heatmaps.shape[3]

        for n in range(coords.shape[0]):
            for p in range(coords.shape[1]):
                hm = heatmaps[n][p]
                px = int(math.floor(coords[n][p][0] + 0.5))
                py = int(math.floor(coords[n][p][1] + 0.5))
                if 1 < px < heatmap_width - 1 and 1 < py < heatmap_height - 1:
                    diff = np.array([
                        hm[py][px + 1] - hm[py][px - 1],
                        hm[py + 1][px] - hm[py - 1][px]
                    ])
                    coords[n][p] += np.sign(diff) * .25
        preds = coords.copy()

        # Transform back
        for i in range(coords.shape[0]):
            preds[i] = transform_preds(coords[i], center[i], scale[i],
                                       [heatmap_width, heatmap_height])

        return preds, maxvals

    def __call__(self, output, center, scale):
        preds, maxvals = self.get_final_preds(output.numpy(), center, scale)
        outputs = [[
            np.concatenate(
                (preds, maxvals), axis=-1), np.mean(
                    maxvals, axis=1)
        ]]
        return outputs
