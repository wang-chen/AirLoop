#!/usr/bin/env python3

import os
import torch
import numpy as np
import kornia as kn
import torch.nn as nn
import kornia.feature as kf
import torch.nn.functional as F
import kornia.geometry.conversions as C

from utils import Visualizer
from models.memory import Memory
from models.featurenet import GridSample
from models.BAnet import ConsecutiveMatch
from models.tool import GlobalStepCounter
from utils import Projector, src_repeat, dst_repeat


class FeatureNetLoss(nn.Module):
    def __init__(self, beta=[1, 1, 5], K=None, writer=None, viz_start=float('inf'), viz_freq=200, counter=None):
        super().__init__()
        self.writer, self.beta, self.counter = writer, beta, counter if counter is not None else GlobalStepCounter()
        self.viz = Visualizer() if self.writer is None else Visualizer('tensorboard', writer=self.writer)
        self.viz_start, self.viz_freq = viz_start, viz_freq
        self.score_corner = ScoreLoss()
        self.desc_match = DiscriptorMatchLoss(writer=writer, viz=self.viz, viz_start=viz_start, viz_freq=viz_freq, counter=self.counter)
        self.gd_match = GlobalDescMatchLoss(writer=writer, viz=self.viz, viz_start=viz_start, viz_freq=viz_freq, counter=self.counter)
        self.projector = Projector()

    def forward(self, net, gd, gd_locs, descriptors, points, scores, score_map, depth_map, poses, K, imgs, env):
        def batch_project(pts):
            return self.projector.cartesian(pts, depth_map, poses, K)

        n_iter = self.counter.steps
        if n_iter >= self.viz_start and n_iter % self.viz_freq == 0:
            self.viz.show(imgs, points, 'hot', values=scores.squeeze(-1).detach().cpu().numpy(), name='train', step=n_iter)

            self.viz.show(score_map, color='hot', name='score', step=n_iter)

            pair = torch.tensor([[0, 1], [0, 3], [0, 5], [0, 7]])
            b_src, b_dst = pair[:, 0], pair[:, 1]
            matched, confidence = ConsecutiveMatch()(descriptors[b_src], descriptors[b_dst], points[b_dst])
            top_conf, top_idx = confidence.topk(50, dim=1)
            top_conf, top_idx = top_conf.detach().cpu().numpy(), top_idx.unsqueeze(-1).repeat(1, 1, 2)
            self.viz.showmatch(imgs[b_src], points[b_src].gather(1, top_idx), imgs[b_dst], matched.gather(1, top_idx), 'hot', top_conf, 0.9, 1, name='match', step=n_iter)

        H, W = score_map.size(2), score_map.size(3)
        # don't use random proposal when calculating gd
        rand_end = -points.shape[1] // (net.module.sample_pass + 1)
        cornerness = self.beta[0] * self.score_corner(score_map, imgs, batch_project)
        proj_pts, invis_idx = batch_project(points)
        match = self.beta[1] * self.desc_match(imgs, descriptors, scores, points.unsqueeze(0), proj_pts, invis_idx, H, W)
        gd_match = self.beta[2] * self.gd_match(net, imgs, gd, gd_locs, points[:, rand_end:], score_map, depth_map, poses, K, descriptors[:, rand_end:], env[0])
        loss = cornerness + match + gd_match

        n_iter = self.counter.steps
        if self.writer is not None:
            self.writer.add_scalars('Loss', {'cornerness': cornerness,
                                             'match': match,
                                             'global': gd_match,
                                             'all': loss}, n_iter)

        return loss


class ScoreLoss(nn.Module):
    def __init__(self, radius=8, num_corners=500):
        super(ScoreLoss, self).__init__()
        self.bceloss = nn.BCELoss()
        self.corner_det = kf.CornerGFTT()
        self.num_corners = num_corners
        self.pool = nn.MaxPool2d(kernel_size=radius, return_indices=True)
        self.unpool = nn.MaxUnpool2d(kernel_size=radius)

    def forward(self, scores_dense, imgs, projector):
        corners = self.get_corners(imgs, projector)
        corners = kn.filters.gaussian_blur2d(corners, kernel_size=(7, 7), sigma=(1, 1))
        lap = kn.filters.laplacian(scores_dense, 5) # smoothness

        return self.bceloss(scores_dense, corners) + (scores_dense * torch.exp(-lap)).mean() * 10

    def get_corners(self, imgs, projector=None):
        (B, _, H, W), N = imgs.shape, self.num_corners
        corners = kf.nms2d(self.corner_det(kn.rgb_to_grayscale(imgs)), (5, 5))

        # only one in patch
        output, indices = self.pool(corners)
        corners = self.unpool(output, indices)

        # keep top
        values, idx = corners.view(B, -1).topk(N, dim=1)
        coords = torch.stack([idx % W, idx // W], dim=2)  # (x, y), same below

        if not projector:
            # keep as-is
            b = torch.arange(0, B).repeat_interleave(N).to(idx)
            h, w = idx // W, idx % W
            values = values.flatten()
        else:
            # combine corners from all images
            coords = kn.normalize_pixel_coordinates(coords, H, W)
            coords, invis_idx = projector(coords)
            coords[tuple(invis_idx)] = -2
            coords_combined = coords.transpose(0, 1).reshape(B, B * N, 2)
            coords_combined = kn.denormalize_pixel_coordinates(coords_combined, H, W).round().to(torch.long)
            b = torch.arange(B).repeat_interleave(B * N).to(coords_combined)
            w, h = coords_combined.reshape(-1, 2).T
            mask = w >= 0
            b, h, w, values = b[mask], h[mask], w[mask], values.flatten().repeat(B)[mask]

        target = torch.zeros_like(corners)
        target[b, 0, h, w] = values
        target = kf.nms2d(target, (5, 5))

        return (target > 0).to(target)


class GlobalDescMatchLoss(nn.Module):
    eps = 1e-2
    logp_min = -12

    def __init__(self, writer=None, sig=1000, n_sample=8,
                 D_point=256, D_frame=256 * 16, n_feature=250, swap_dir='./memory',
                 viz=None, viz_start=float('inf'), viz_freq=200, counter=None, debug=False):
        super().__init__()
        self.writer = writer
        self.memory = Memory(D_point, D_frame, n_feature, swap_dir)
        self.projector = Projector()
        self.grid_sample = GridSample()
        self.cosine = PairwiseCosine(inter_batch=True)
        self.counter = counter if counter is not None else GlobalStepCounter()
        self.sig, self.n_sample, self.swap_dir = sig, n_sample, swap_dir
        self.viz, self.viz_start, self.viz_freq = viz, viz_start, viz_freq
        self.debug = debug

    def forward(self, net, imgs, gd, gd_locs, points, score_map, depth_map, pose, K, descriptors, env):
        self.memory.swap(env)

        points = (torch.rand_like(points) - 0.5) * 2
        points_w = self.projector.pix2world(points, depth_map, pose, K)

        if len(self.memory) > 0:
            B, _, H, W = depth_map.shape
            _, mem_desc, mem_pos = self.memory.sample_frames(self.n_sample)

            relevance, mem_pts_scr = feature_pt_ncovis(mem_pos, points, depth_map, pose, K, self.projector, self.grid_sample,
                                     self.eps, True)
            relevance = (251 - relevance).sqrt()
            # sim = 1 - self.cosine(net.module.global_desc(mem_desc).unsqueeze(1), gd.unsqueeze(1))[:, :, 0, 0].clamp(min=0)
            sim = torch.cdist(net.module.global_desc(mem_desc)[0], gd)

            loss = (torch.log(sim.unsqueeze(1) / sim.unsqueeze(2)) -
                    torch.log(relevance.unsqueeze(1) / relevance.unsqueeze(2)).clamp(min=-10, max=10))**2
            gd_loc_sp = torch.norm(gd_locs, p=1, dim=-1).mean(dim=-1)
            gd_norm = torch.norm(gd, dim=-1)
            loss = loss.mean(dim=(1, 2)) + gd_norm * 1e-4 + gd_loc_sp * 1e-4
        else:
            loss = torch.zeros(1).to(imgs)

        n_iter = self.counter.steps
        if self.writer is not None:
            self.writer.add_scalars('Misc/MemoryUsage', {'len': len(self.memory)}, n_iter)
            if len(self.memory) > 0:
                self.writer.add_histogram('Misc/FrameRelevance', relevance, n_iter)
                self.writer.add_scalars('Misc/GD', {'LocSparsity': gd_loc_sp.mean() / gd.shape[1], '2-Norm': gd_norm.mean()}, n_iter)

        if len(self.memory) > 0 and n_iter >= self.viz_start and n_iter % self.viz_freq == 0:
            B, N, _ = points.shape
            proj_pts_ = mem_pts_scr.reshape(self.n_sample, B, N, 2).permute(1, 0, 2, 3).reshape(B, self.n_sample*N, 2)
            proj_pts_color = torch.arange(self.n_sample + 1)[None, :, None].expand(B, self.n_sample+1, N)
            proj_pts_color = proj_pts_color.reshape(B, (self.n_sample + 1)* N).detach().cpu().numpy()
            self.viz.show(imgs, torch.cat([points, proj_pts_], 1), 'tab10', values=proj_pts_color,
                        vmin=0, vmax=10, name='Misc/GlobalDesc/Proj', step=n_iter)

        self.memory.store(gd, descriptors, points_w)

        return loss.mean()


def feature_pt_ncovis(pos0, pts1, depth1, pose1, K1, projector, grid_sample, eps=1e-2, ret_proj=False):
    B0, B1 = len(pos0), len(pts1)
    _, _, H, W = depth1.shape

    # find where points from other frames land
    pts0_scr1, pts0_depth1 = projector.world2pix(src_repeat(pos0, B1), (H, W),
        dst_repeat(pose1, B0), dst_repeat(K1, B0))
    pts0_scr1_depth1 = grid_sample((dst_repeat(depth1, B0), pts0_scr1)).squeeze(-1)
    pts0_scr1[(pts0_depth1 < 0) | (pts0_depth1 > pts0_scr1_depth1 + eps) | ((pts0_scr1.abs() > 1).any(dim=-1))] = np.nan

    n_covis = pts0_scr1.isfinite().all(dim=-1).sum(dim=-1).to(torch.float)

    if ret_proj:
        return n_covis.reshape(B0, B1), pts0_scr1
    return n_covis.reshape(B0, B1)


class DiscriptorMatchLoss(nn.Module):
    eps = 1e-6

    def __init__(self, radius=1, writer=None, viz=None, viz_start=float('inf'), viz_freq=200, counter=None, debug=False):
        super(DiscriptorMatchLoss, self).__init__()
        self.radius, self.writer, self.counter = radius, writer, counter if counter is not None else GlobalStepCounter()
        self.cosine = PairwiseCosine(inter_batch=True)
        self.viz, self.viz_start, self.viz_freq = viz, viz_start, viz_freq
        self.debug = debug

    def forward(self, images, descriptors, scores, pts_src, pts_dst, invis_idx, height, width):
        pts_src = C.denormalize_pixel_coordinates(pts_src.detach(), height, width)
        pts_dst = C.denormalize_pixel_coordinates(pts_dst.detach(), height, width)

        dist = torch.cdist(pts_dst, pts_src)
        dist[tuple(invis_idx)] = float('nan')
        pcos = self.cosine(descriptors, descriptors)

        match = (dist <= self.radius).triu(diagonal=1)
        miss = (dist > self.radius).triu(diagonal=1)

        scores = scores.detach()
        score_ave = (scores[:, None, :, None] + scores[None, :, None, :]).clamp(min=self.eps) / 2
        pcos = self.cosine(descriptors, descriptors)

        sig_match = -torch.log(score_ave[match])
        sig_miss  = -torch.log(score_ave[miss])

        s_match = pcos[match]
        s_miss = pcos[miss]

        n_iter = self.counter.steps
        if self.writer is not None:
            self.writer.add_scalars('Misc/DiscriptorMatch/Count', {
                'n_match': match.sum(),
                'n_miss': miss.sum(),
            }, n_iter)

            if len(sig_match) > 0:
                self.writer.add_histogram('Misc/DiscriptorMatch/Sim/match', s_match, n_iter)
                self.writer.add_histogram('Misc/DiscriptorMatch/Sim/miss', s_miss[:len(s_match)], n_iter)

        # match/mismatch blending factor
        f = lambda d: 0.75 - (d - self.radius) * (d + self.radius) / (4 * self.radius**2)

        alpha_match = f(dist[match].clamp(max=self.radius * 2))
        alpha_miss = f(dist[miss].clamp(max=self.radius * 2))

        loss_match, loss_miss = self.nll(sig_match, s_match, alpha_match), self.nll(sig_miss, s_miss, alpha_miss)

        loss_miss, loss_miss_idx = loss_miss.topk(match.sum() * 2)

        if self.debug and n_iter >= self.viz_start and n_iter % self.viz_freq == 0:
            B, *shape = images.shape
            src_images = images.unsqueeze(1).expand(B, B, *shape).reshape(B**2, *shape)
            dst_images = images.unsqueeze(0).expand(B, B, *shape).reshape(B**2, *shape)

            N = pts_src.shape[2]

            pts_src = pts_src.squeeze(0)
            src_pts_ = C.normalize_pixel_coordinates(pts_src, height, width)
            src_pts = torch.zeros(B**2, N * 2, 2).to(src_pts_).fill_(np.nan)
            dst_pts = torch.zeros(B**2, N * 2, 2).to(src_pts_).fill_(np.nan)
            vals = torch.zeros(B * B, N * 2).to(scores).fill_(np.nan)

            # match pairs
            match_b, match_src, match_dst = match.reshape(B**2, N, N).nonzero(as_tuple=True)
            src_pts[match_b, match_src] = src_pts_[match_b // B, match_src]
            dst_pts[match_b, match_src] = src_pts_[match_b % B, match_dst]
            vals[match_b, match_src] = 2 - loss_match

            # miss pairs
            miss_b, miss_src, miss_dst = miss.reshape(B**2, N, N).nonzero(as_tuple=True)
            miss_b, miss_src, miss_dst = miss_b[loss_miss_idx], miss_src[loss_miss_idx], miss_dst[loss_miss_idx]
            src_pts[miss_b, miss_src + N] = src_pts_[miss_b // B, miss_src]
            dst_pts[miss_b, miss_src + N] = src_pts_[miss_b % B, miss_dst]
            vals[miss_b, miss_src + N] = loss_miss - 2

            vrange = vals[vals.isfinite()].abs().max()
            self.viz.showmatch(src_images, src_pts, dst_images, dst_pts,
                'gnuplot2', vals.detach().cpu().numpy(), -vrange, vrange, name='desc', step=n_iter, nrow=B)

        return loss_match.mean() + loss_miss.mean()

    def nll(self, sig, cos, match=1, topk=None):
        # p(x) = exp(-l / sig) * C; l = 1 - x if match else x
        norm_const = torch.log(sig * (1 - torch.exp(-1 / sig)))
        loss = ((1 - cos) * match + cos * (1 - match)) / sig + norm_const
        return loss


class PairwiseCosine(nn.Module):
    def __init__(self, inter_batch=False, dim=-1, eps=1e-8):
        super(PairwiseCosine, self).__init__()
        self.inter_batch, self.dim, self.eps = inter_batch, dim, eps
        self.eqn = 'amd,bnd->abmn' if inter_batch else 'bmd,bnd->bmn'

    def forward(self, x, y):
        xx = torch.sum(x**2, dim=self.dim).unsqueeze(-1) # (A, M, 1)
        yy = torch.sum(y**2, dim=self.dim).unsqueeze(-2) # (B, 1, N)
        if self.inter_batch:
            xx, yy = xx.unsqueeze(1), yy.unsqueeze(0) # (A, 1, M, 1), (1, B, 1, N)
        xy = torch.einsum(self.eqn, x, y) if x.shape[1] > 0 else torch.zeros_like(xx * yy)
        return xy / (xx * yy).clamp(min=self.eps**2).sqrt()
