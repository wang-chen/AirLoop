#!/usr/bin/env python3

import math
import torch
import torch.nn as nn
from kornia.feature import nms
from torchvision import models
import torch.nn.functional as F
import kornia.geometry.conversions as C


class IndexSelect(nn.Module):
    def __init__(self, dim, index):
        super().__init__()
        self.dim, self.index = dim, index

    def forward(self, x):
        self.index = self.index.to(x.device)
        return x.index_select(self.dim, self.index)


class ConstantBorder(nn.Module):
    '''
    Set Boarders to Constant
    '''
    def __init__(self, border=4, value=-math.inf):
        super().__init__()
        self.pad1 = nn.ConstantPad2d(-border, value=value)
        self.pad2 = nn.ConstantPad2d(border, value=value)

    def forward(self, x):
        return self.pad2(self.pad1(x))


class GridSample(nn.Module):
    def __init__(self, mode='bilinear'):
        super().__init__()
        self.mode = mode

    def forward(self, inputs):
        features, points = inputs
        dim = len(points.shape)
        points = points.view(features.size(0),1,-1,2) if dim == 3 else points
        output = F.grid_sample(features, points, self.mode, align_corners=True).permute(0,2,3,1)
        return output.squeeze(1) if dim == 3 else output


class GraphAttn(nn.Module):
    def __init__(self, in_features, out_features, alpha, dropout=0.5, beta=0.2):
        super().__init__()
        self.alpha = alpha
        self.tran = nn.Linear(in_features, out_features, bias=False)
        self.att1 = nn.Linear(out_features, 1, bias=False)
        self.att2 = nn.Linear(out_features, 1, bias=False)
        self.norm = nn.Sequential(nn.Softmax(dim=-1), nn.Dropout(dropout))
        self.leakyrelu = nn.LeakyReLU(beta)

    def forward(self, x):
        h = self.tran(x)
        att = self.att1(h) + self.att2(h).permute(0,2,1)
        adj = self.norm(self.leakyrelu(att.squeeze()))
        return self.alpha * h + (1-self.alpha) * adj @ h


class FeatureNet(models.VGG):
    def __init__(self, feat_dim=256, feat_num=500):
        super().__init__(models.vgg13().features)
        self.feat_dim, self.feat_num = feat_dim, feat_num
        # Only adopt the first 15 layers of pre-trained vgg13. Feature Map: (512, H/8, W/8)
        self.load_state_dict(models.vgg13(pretrained=True).state_dict())
        self.features = nn.Sequential(*list(self.features.children())[:15])
        del self.classifier

        self.scores = nn.Sequential(
                nn.Conv2d(256, 128, kernel_size=3, stride=1, padding=1), nn.ReLU(),
                nn.Conv2d(128, 65, kernel_size=1, stride=1, padding=0), nn.Softmax(dim=1),
                IndexSelect(dim=1, index=torch.arange(64)),
                nn.PixelShuffle(upscale_factor=8),
                ConstantBorder(border=4, value=0))
        self.nms = nms.NonMaximaSuppression2d((7, 7))

        self.descriptors = nn.Sequential(
                nn.Conv2d(256, self.feat_dim, kernel_size=3, stride=1, padding=1), nn.ReLU(),
                nn.Conv2d(self.feat_dim, self.feat_dim, kernel_size=1, stride=1, padding=0))
        self.sample = nn.Sequential(GridSample(), nn.BatchNorm1d(self.feat_num))
        self.encoder = nn.Sequential(nn.Linear(3, 256), nn.ReLU(), nn.Linear(256, self.feat_dim))
        self.residual = nn.Sequential(
                nn.Conv2d(3, 128, kernel_size=9, padding=4), nn.ReLU(),
                nn.Conv2d(128, self.feat_dim, kernel_size=3, padding=1))
        self.graph = nn.Sequential(
                GraphAttn(self.feat_dim, self.feat_dim, alpha=0.9), nn.ReLU(),
                GraphAttn(self.feat_dim, self.feat_dim, alpha=0.9))

    def forward(self, inputs):

        B, _, H, W = inputs.shape

        features = self.features(inputs)

        pointness = self.scores(features)

        scores, points = self.nms(pointness).view(B,-1,1).topk(self.feat_num, dim=1)

        points = torch.cat((points%W, points//W), dim=-1)

        points = C.normalize_pixel_coordinates(points, H, W)

        descriptors = self.descriptors(features)

        residual = self.residual(inputs)

        descriptors = self.sample((descriptors, points)) + self.sample((residual, points))

        descriptors = descriptors + self.encoder(torch.cat([points, scores], dim=-1))

        descriptors = self.graph(descriptors)

        return descriptors, points, pointness if self.training else scores


if __name__ == "__main__":
    '''Test codes'''
    import argparse
    from tool import Timer

    parser = argparse.ArgumentParser(description='Test FeatureNet')
    parser.add_argument("--device", type=str, default='cuda', help="cuda, cuda:0, or cpu")
    parser.add_argument('--seed', type=int, default=0, help='Random seed.')
    parser.add_argument("--batch-size", type=int, default=10, help="number of minibatch size")
    parser.add_argument('--crop-size', nargs='+', type=int, default=[320,320], help='image crop size')
    args = parser.parse_args()
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)

    net = FeatureNet(512, 200).to(args.device).eval()
    inputs = torch.randn(args.batch_size,3,*args.crop_size).to(args.device)

    timer = Timer()
    with torch.no_grad():
        for i in range(5):
            descriptors, points, scores = net(inputs)
            print(i, 'D:',descriptors.shape, 'P:',points.shape, 'S:',scores.shape)
    print('time:', timer.end())
