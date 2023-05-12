import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import os
import sys

from .blocks import MLP, ResidualBlock, UBlock


class ProposalModule(nn.Module):
    def __init__(self, in_channels=32, out_channels=6, num_layers=2):
        super().__init__()
        self.fc = nn.Sequential(
            nn.Linear(in_channels, in_channels),
            # nn.BatchNorm1d(num_features=in_channels,eps=1e-4, momentum=0.1),
            nn.ReLU(),
            nn.Linear(in_channels, out_channels),
        )

    def init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                nn.init.constant_(m.bias, 0)
        nn.init.normal_(self[-1].weight, 0, 0.01)
        nn.init.constant_(self[-1].bias, 0)

    def forward(self, data_dict):
        # bbox_features为detection module生成的instance features
        # pred_bboxes为预测出的bbox参数（中心和三维）
        data_dict["pred_bboxes"] = self.fc(data_dict['select_feats'])  # (B,6)，3维中心坐标和3维尺寸
        data_dict['bbox_loss'] = self.compute_loss(data_dict["pred_bboxes"], data_dict["ref_center_label"],
                                                   data_dict["ref_size_label"], data_dict['good_clu_masks'])

        return data_dict

    def compute_loss(self, pred_bboxes, ref_center, ref_size, good_clu_masks):  # (B,6),(B,3),(B,3)

        # mask out bad clusters
        good_bbox_masks = good_clu_masks.unsqueeze(1).repeat(1, 3)  # (B, 3)
        pred_center = pred_bboxes[:, 0:3] * good_bbox_masks
        pred_size = pred_bboxes[:, 3:6] * good_bbox_masks
        ref_center = ref_center * good_bbox_masks
        ref_size = ref_size * good_bbox_masks

        # calculate L2 Loss

        mse = torch.nn.MSELoss()
        loss = mse(pred_center, ref_center) + mse(pred_size, ref_size)
        return loss
# test
# test_mlp = ProposalModule()
# print(test_mlp)
# test_mlp.eval()
# input=torch.rand(1,500,32)
# output = test_mlp.fc(input)
# print(output)
# print(output.shape)
