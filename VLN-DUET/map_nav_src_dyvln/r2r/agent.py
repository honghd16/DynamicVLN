import json
import os
import sys
import numpy as np
import random
import math
import time
from collections import defaultdict
import line_profiler

import torch
import torch.nn as nn
from torch import optim
import torch.nn.functional as F

from utils.distributed import is_default_gpu
from utils.ops import pad_tensors, gen_seq_masks
from torch.nn.utils.rnn import pad_sequence

from .agent_base import Seq2SeqAgent
from .eval_utils import cal_dtw

from models.graph_utils import GraphMap
from models.model import VLNBert, Critic
from models.ops import pad_tensors_wgrad

def cal_distance(node_1, node_2):
    difference = np.array(node_1) - np.array(node_2)
    distance = np.sqrt(np.sum(difference**2))
    return distance

class GMapNavAgent(Seq2SeqAgent):
    
    def _build_model(self):
        self.vln_bert = VLNBert(self.args).cuda()
        self.critic = Critic(self.args).cuda()
        # buffer
        self.scanvp_cands = {}

    def _language_variable(self, obs):
        seq_lengths = [len(ob['instr_encoding']) for ob in obs]
        
        seq_tensor = np.zeros((len(obs), max(seq_lengths)), dtype=np.int64)
        mask = np.zeros((len(obs), max(seq_lengths)), dtype=np.bool)
        for i, ob in enumerate(obs):
            seq_tensor[i, :seq_lengths[i]] = ob['instr_encoding']
            mask[i, :seq_lengths[i]] = True

        seq_tensor = torch.from_numpy(seq_tensor).long().cuda()
        mask = torch.from_numpy(mask).cuda()
        return {
            'txt_ids': seq_tensor, 'txt_masks': mask
        }

    def _panorama_feature_variable(self, obs):
        ''' Extract precomputed features into variable. '''
        batch_view_img_fts, batch_loc_fts, batch_nav_types = [], [], []
        batch_view_lens, batch_cand_vpids = [], []
        batch_seen_vpids = []
        
        for i, ob in enumerate(obs):
            view_img_fts, view_ang_fts, nav_types, cand_vpids = [], [], [], []
            seen_vpids = []
            # cand views
            used_viewidxs = set()
            for j, cc in enumerate(ob['candidate']):
                view_img_fts.append(cc['feature'][:self.args.image_feat_size])
                view_ang_fts.append(cc['feature'][self.args.image_feat_size:])
                nav_types.append(1)
                cand_vpids.append(cc['viewpointId'])
                used_viewidxs.add(cc['pointId'])
            for j, cc in enumerate(ob['seen_node']):
                view_img_fts.append(cc['feature'][:self.args.image_feat_size])
                view_ang_fts.append(cc['feature'][self.args.image_feat_size:])
                nav_types.append(1)
                seen_vpids.append(cc['viewpointId'])
                used_viewidxs.add(cc['pointId'])
            # non cand views
            view_img_fts.extend([x[:self.args.image_feat_size] for k, x \
                in enumerate(ob['feature']) if k not in used_viewidxs])
            view_ang_fts.extend([x[self.args.image_feat_size:] for k, x \
                in enumerate(ob['feature']) if k not in used_viewidxs])
            nav_types.extend([0] * (36 - len(used_viewidxs)))
            # combine cand views and noncand views
            view_img_fts = np.stack(view_img_fts, 0)    # (n_views, dim_ft)
            view_ang_fts = np.stack(view_ang_fts, 0)
            view_box_fts = np.array([[1, 1, 1]] * len(view_img_fts)).astype(np.float32)
            view_loc_fts = np.concatenate([view_ang_fts, view_box_fts], 1)
            
            batch_view_img_fts.append(torch.from_numpy(view_img_fts))
            batch_loc_fts.append(torch.from_numpy(view_loc_fts))
            batch_nav_types.append(torch.LongTensor(nav_types))
            batch_cand_vpids.append(cand_vpids)
            batch_seen_vpids.append(seen_vpids)
            batch_view_lens.append(len(view_img_fts))

        # pad features to max_len
        batch_view_img_fts = pad_tensors(batch_view_img_fts).cuda()
        batch_loc_fts = pad_tensors(batch_loc_fts).cuda()
        batch_nav_types = pad_sequence(batch_nav_types, batch_first=True, padding_value=0).cuda()
        batch_view_lens = torch.LongTensor(batch_view_lens).cuda()

        return {
            'view_img_fts': batch_view_img_fts, 'loc_fts': batch_loc_fts, 
            'nav_types': batch_nav_types, 'view_lens': batch_view_lens, 
            'cand_vpids': batch_cand_vpids,
            'seen_vpids': batch_seen_vpids,
        }

    def _panorama_feature_variable_search(self, ob):
        view_img_fts, view_ang_fts, nav_types, cand_vpids = [], [], [], []
        seen_vpids = []
        # cand views
        used_viewidxs = set()
        for j, cc in enumerate(ob['candidate']):
            view_img_fts.append(cc['feature'][:self.args.image_feat_size])
            view_ang_fts.append(cc['feature'][self.args.image_feat_size:])
            nav_types.append(1)
            cand_vpids.append(cc['viewpointId'])
            used_viewidxs.add(cc['pointId'])
        for j, cc in enumerate(ob['seen_node']):
            view_img_fts.append(cc['feature'][:self.args.image_feat_size])
            view_ang_fts.append(cc['feature'][self.args.image_feat_size:])
            nav_types.append(1)
            seen_vpids.append(cc['viewpointId'])
            used_viewidxs.add(cc['pointId'])
        # non cand views
        view_img_fts.extend([x[:self.args.image_feat_size] for k, x \
            in enumerate(ob['feature']) if k not in used_viewidxs])
        view_ang_fts.extend([x[self.args.image_feat_size:] for k, x \
            in enumerate(ob['feature']) if k not in used_viewidxs])
        nav_types.extend([0] * (36 - len(used_viewidxs)))
        # combine cand views and noncand views
        view_img_fts = np.stack(view_img_fts, 0)    # (n_views, dim_ft)
        view_ang_fts = np.stack(view_ang_fts, 0)
        view_box_fts = np.array([[1, 1, 1]] * len(view_img_fts)).astype(np.float32)
        view_loc_fts = np.concatenate([view_ang_fts, view_box_fts], 1)
        
        view_lens = torch.LongTensor([len(view_img_fts)]).cuda()
        view_img_fts = torch.from_numpy(view_img_fts).unsqueeze(0).cuda()
        loc_fts = torch.from_numpy(view_loc_fts).unsqueeze(0).cuda()
        nav_types = torch.LongTensor(nav_types).unsqueeze(0).cuda()

        return {
            'view_img_fts': view_img_fts, 'loc_fts': loc_fts, 
            'nav_types': nav_types, 'view_lens': view_lens, 
            'cand_vpids': cand_vpids,
            'seen_vpids': seen_vpids,
        }

    def _nav_gmap_variable(self, obs, gmaps):
        # [stop] + gmap_vpids
        batch_size = len(obs)
        
        batch_gmap_vpids, batch_gmap_lens = [], []
        batch_gmap_img_embeds, batch_gmap_step_ids, batch_gmap_pos_fts = [], [], []
        batch_gmap_pair_dists, batch_gmap_visited_masks = [], []
        batch_no_vp_left = []
        seen_ids = []
        for i, gmap in enumerate(gmaps):
            visited_vpids, unvisited_vpids = [], []                
            seen_vp = []
            seen_id = []
            for k in gmap.node_positions.keys():
                if self.args.act_visited_nodes:
                    if k == obs[i]['viewpoint']:
                        visited_vpids.append(k)
                    else:
                        unvisited_vpids.append(k)
                else:
                    if gmap.graph.visited(k):
                        visited_vpids.append(k)
                    else:
                        if gmap.graph.distance(obs[i]['viewpoint'], k) >= 1e7:
                            seen_vp.append(k)
                        unvisited_vpids.append(k)
            if len(unvisited_vpids) == 0 or all('seen_node' in vpid for vpid in unvisited_vpids):
                batch_no_vp_left.append(True)
            else:
                batch_no_vp_left.append(False)
            if self.args.enc_full_graph:
                gmap_vpids = [None] + visited_vpids + unvisited_vpids
                gmap_visited_masks = [0] + [1] * len(visited_vpids) + [0] * len(unvisited_vpids)
            else:
                gmap_vpids = [None] + unvisited_vpids
                gmap_visited_masks = [0] * len(gmap_vpids)
            
            seen_id = [gmap_vpids.index(x) for x in seen_vp]
            seen_ids.append(seen_id)

            gmap_step_ids = [gmap.node_step_ids.get(vp, 0) for vp in gmap_vpids]
            gmap_img_embeds = [gmap.get_node_embed(vp) for vp in gmap_vpids[1:]]
            gmap_img_embeds = torch.stack(
                [torch.zeros_like(gmap_img_embeds[0])] + gmap_img_embeds, 0
            )   # cuda

            gmap_pos_fts = gmap.get_pos_fts(
                obs[i]['viewpoint'], gmap_vpids, obs[i]['heading'], obs[i]['elevation'],
            )

            gmap_pair_dists = np.zeros((len(gmap_vpids), len(gmap_vpids)), dtype=np.float32)
            for i in range(1, len(gmap_vpids)):
                for j in range(i+1, len(gmap_vpids)):
                    gmap_pair_dists[i, j] = gmap_pair_dists[j, i] = \
                        gmap.graph.distance(gmap_vpids[i], gmap_vpids[j])

            batch_gmap_img_embeds.append(gmap_img_embeds)
            batch_gmap_step_ids.append(torch.LongTensor(gmap_step_ids))
            batch_gmap_pos_fts.append(torch.from_numpy(gmap_pos_fts))
            batch_gmap_pair_dists.append(torch.from_numpy(gmap_pair_dists))
            batch_gmap_visited_masks.append(torch.BoolTensor(gmap_visited_masks))
            batch_gmap_vpids.append(gmap_vpids)
            batch_gmap_lens.append(len(gmap_vpids))

        # collate
        batch_gmap_lens = torch.LongTensor(batch_gmap_lens)
        batch_gmap_masks = gen_seq_masks(batch_gmap_lens).cuda()
        batch_gmap_img_embeds = pad_tensors_wgrad(batch_gmap_img_embeds)
        batch_gmap_step_ids = pad_sequence(batch_gmap_step_ids, batch_first=True).cuda()
        batch_gmap_pos_fts = pad_tensors(batch_gmap_pos_fts).cuda()
        batch_gmap_visited_masks = pad_sequence(batch_gmap_visited_masks, batch_first=True).cuda()

        max_gmap_len = max(batch_gmap_lens)
        gmap_pair_dists = torch.zeros(batch_size, max_gmap_len, max_gmap_len).float()
        for i in range(batch_size):
            gmap_pair_dists[i, :batch_gmap_lens[i], :batch_gmap_lens[i]] = batch_gmap_pair_dists[i]
        gmap_pair_dists = gmap_pair_dists.cuda()

        # generate masks to mask the seen nodes
        seen_mask = torch.ones([batch_size, max_gmap_len], dtype=torch.bool).cuda()
        for i, indices in enumerate(seen_ids):
            seen_mask[i, indices] = 0

        return {
            'gmap_vpids': batch_gmap_vpids, 'gmap_img_embeds': batch_gmap_img_embeds, 
            'gmap_step_ids': batch_gmap_step_ids, 'gmap_pos_fts': batch_gmap_pos_fts,
            'gmap_visited_masks': batch_gmap_visited_masks, 
            'gmap_pair_dists': gmap_pair_dists, 'gmap_masks': batch_gmap_masks,
            'no_vp_left': batch_no_vp_left, 'seen_mask': seen_mask,
        }

    def _nav_gmap_variable_search(self, ob, gmap):
        visited_vpids, unvisited_vpids = [], []                
        seen_vp = []
        seen_id = []
        for k in gmap.node_positions.keys():
            if self.args.act_visited_nodes:
                if k == ob['viewpoint']:
                    visited_vpids.append(k)
                else:
                    unvisited_vpids.append(k)
            else:
                if gmap.graph.visited(k):
                    visited_vpids.append(k)
                else:
                    if gmap.graph.distance(ob['viewpoint'], k) >= 1e7:
                        seen_vp.append(k)
                    unvisited_vpids.append(k)
        if len(unvisited_vpids) == 0 or all('seen_node' in vpid for vpid in unvisited_vpids):
            no_vp_left = True
        else:
            no_vp_left = False
        if self.args.enc_full_graph:
            gmap_vpids = [None] + visited_vpids + unvisited_vpids
            gmap_visited_masks = [0] + [1] * len(visited_vpids) + [0] * len(unvisited_vpids)
        else:
            gmap_vpids = [None] + unvisited_vpids
            gmap_visited_masks = [0] * len(gmap_vpids)
        
        seen_id = [gmap_vpids.index(x) for x in seen_vp]

        gmap_step_ids = [gmap.node_step_ids.get(vp, 0) for vp in gmap_vpids]
        gmap_img_embeds = [gmap.get_node_embed(vp) for vp in gmap_vpids[1:]]
        gmap_img_embeds = torch.stack(
            [torch.zeros_like(gmap_img_embeds[0])] + gmap_img_embeds, 0
        )   # cuda

        gmap_pos_fts = gmap.get_pos_fts(
            ob['viewpoint'], gmap_vpids, ob['heading'], ob['elevation'],
        )

        gmap_pair_dists = np.zeros((len(gmap_vpids), len(gmap_vpids)), dtype=np.float32)
        for i in range(1, len(gmap_vpids)):
            for j in range(i+1, len(gmap_vpids)):
                gmap_pair_dists[i, j] = gmap_pair_dists[j, i] = \
                    gmap.graph.distance(gmap_vpids[i], gmap_vpids[j])

        gmap_step_ids = torch.LongTensor(gmap_step_ids).unsqueeze(0).cuda()
        gmap_pos_fts = torch.from_numpy(gmap_pos_fts).unsqueeze(0).cuda()
        gmap_pair_dists = torch.from_numpy(gmap_pair_dists)
        gmap_visited_masks = torch.BoolTensor(gmap_visited_masks).unsqueeze(0).cuda()
        gmap_lens = torch.LongTensor([len(gmap_vpids)])
        gmap_masks = gen_seq_masks(gmap_lens).cuda()
        gmap_img_embeds = gmap_img_embeds.unsqueeze(0)

        max_gmap_len = max(gmap_lens)
        gmap_pair_dists = torch.zeros(1, max_gmap_len, max_gmap_len).float()
        gmap_pair_dists[0, :gmap_lens[0], :gmap_lens[0]] = gmap_pair_dists[0]
        gmap_pair_dists = gmap_pair_dists.cuda()

        # generate masks to mask the seen nodes
        seen_mask = torch.ones([1, max_gmap_len], dtype=torch.bool).cuda()
        seen_mask[0, seen_id] = 0

        return {
            'gmap_vpids': gmap_vpids, 'gmap_img_embeds': gmap_img_embeds, 
            'gmap_step_ids':gmap_step_ids, 'gmap_pos_fts': gmap_pos_fts,
            'gmap_visited_masks': gmap_visited_masks, 
            'gmap_pair_dists': gmap_pair_dists, 'gmap_masks': gmap_masks,
            'no_vp_left': no_vp_left, 'seen_mask': seen_mask
        }

    def _nav_vp_variable(self, obs, gmaps, pano_embeds, cand_vpids, view_lens, nav_types):
        batch_size = len(obs)

        # add [stop] token
        vp_img_embeds = torch.cat(
            [torch.zeros_like(pano_embeds[:, :1]), pano_embeds], 1
        )

        batch_vp_pos_fts = []
        for i, gmap in enumerate(gmaps):
            cur_cand_pos_fts = gmap.get_pos_fts(
                obs[i]['viewpoint'], cand_vpids[i], 
                obs[i]['heading'], obs[i]['elevation']
            )
            cur_start_pos_fts = gmap.get_pos_fts(
                obs[i]['viewpoint'], [gmap.start_vp], 
                obs[i]['heading'], obs[i]['elevation']
            )                    
            # add [stop] token at beginning
            vp_pos_fts = np.zeros((vp_img_embeds.size(1), 14), dtype=np.float32)
            vp_pos_fts[:, :7] = cur_start_pos_fts
            vp_pos_fts[1:len(cur_cand_pos_fts)+1, 7:] = cur_cand_pos_fts
            batch_vp_pos_fts.append(torch.from_numpy(vp_pos_fts))

        batch_vp_pos_fts = pad_tensors(batch_vp_pos_fts).cuda()

        vp_nav_masks = torch.cat([torch.ones(batch_size, 1).bool().cuda(), nav_types == 1], 1)

        return {
            'vp_img_embeds': vp_img_embeds,
            'vp_pos_fts': batch_vp_pos_fts,
            'vp_masks': gen_seq_masks(view_lens+1),
            'vp_nav_masks': vp_nav_masks,
            'vp_cand_vpids': [[None]+x for x in cand_vpids],
        }

    def _nav_vp_variable_search(self, ob, gmap, pano_embeds, cand_vpids, view_lens, nav_types):
        # add [stop] token
        vp_img_embeds = torch.cat(
            [torch.zeros_like(pano_embeds[:, :1]), pano_embeds], 1
        )

        cur_cand_pos_fts = gmap.get_pos_fts(
            ob['viewpoint'], cand_vpids, 
            ob['heading'], ob['elevation']
        )
        cur_start_pos_fts = gmap.get_pos_fts(
            ob['viewpoint'], [gmap.start_vp], 
            ob['heading'], ob['elevation']
        )                    
        # add [stop] token at beginning
        vp_pos_fts = np.zeros((vp_img_embeds.size(1), 14), dtype=np.float32)
        vp_pos_fts[:, :7] = cur_start_pos_fts
        vp_pos_fts[1:len(cur_cand_pos_fts)+1, 7:] = cur_cand_pos_fts
        vp_pos_fts =  torch.from_numpy(vp_pos_fts).unsqueeze(0).cuda()

        vp_nav_masks = torch.cat([torch.ones(1, 1).bool().cuda(), nav_types == 1], 1)

        return {
            'vp_img_embeds': vp_img_embeds,
            'vp_pos_fts': vp_pos_fts,
            'vp_masks': gen_seq_masks(view_lens+1),
            'vp_nav_masks': vp_nav_masks,
            'vp_cand_vpids': [None]+cand_vpids,
        }

    def _teacher_action(self, obs, vpids, ended, visited_masks=None):
        """
        Extract teacher actions into variable.
        :param obs: The observation.
        :param ended: Whether the action seq is ended
        :return:
        """
        a = np.zeros(len(obs), dtype=np.int64)
        for i, ob in enumerate(obs):
            if ended[i]:                                            # Just ignore this index
                a[i] = self.args.ignoreid
            else:
                if ob['viewpoint'] == ob['gt_path'][-1]:
                    a[i] = 0    # Stop if arrived 
                else:
                    scan = ob['scan']
                    cur_vp = ob['viewpoint']
                    min_idx, min_dist = self.args.ignoreid, float('inf')
                    for j, vpid in enumerate(vpids[i]):
                        if j > 0 and ((visited_masks is None) or (not visited_masks[i][j])):
                            # dist = min([self.env.shortest_distances[scan][vpid][end_vp] for end_vp in ob['gt_end_vps']])
                            dist = self.env.shortest_distances[(i,scan)][vpid][ob['gt_path'][-1]] \
                                    + self.env.shortest_distances[(i,scan)][cur_vp][vpid]
                            if dist < min_dist:
                                min_dist = dist
                                min_idx = j
                    a[i] = min_idx
                    if min_idx == self.args.ignoreid:
                        print('scan %s: all vps are searched' % (scan))

        return torch.from_numpy(a).cuda()

    def _teacher_action_r4r(
        self, gmaps, obs, vpids, ended, visited_masks=None, imitation_learning=False, t=None, traj=None
    ):
        """R4R is not the shortest path. The goal location can be visited nodes.
        """
        a = np.zeros(len(obs), dtype=np.int64)
        for i, ob in enumerate(obs):
            if ended[i]:                                            # Just ignore this index
                a[i] = self.args.ignoreid
            else:
                if imitation_learning:
                    assert ob['viewpoint'] == ob['gt_path'][t]
                    if t == len(ob['gt_path']) - 1:
                        a[i] = 0    # stop
                    else:
                        goal_vp = ob['gt_path'][t + 1]
                        for j, vpid in enumerate(vpids[i]):
                            if goal_vp == vpid:
                                a[i] = j
                                break
                else:
                    if ob['viewpoint'] == ob['gt_path'][-1]:
                        a[i] = 0    # Stop if arrived 
                    else:
                        scan = ob['scan']
                        cur_vp = ob['viewpoint']
                        min_idx, min_dist = self.args.ignoreid, float('inf')
                        for j, vpid in enumerate(vpids[i]):
                            if gmaps[i].graph.distance(ob['viewpoint'], vpid) >= 1e7:
                                continue
                            if j > 0 and ((visited_masks is None) or (not visited_masks[i][j])):
                                if self.args.expert_policy == 'ndtw':
                                    dist = - cal_dtw(
                                        self.env.shortest_distances[scan], 
                                        sum(traj[i]['path'], []) + self.env.shortest_paths[scan][ob['viewpoint']][vpid][1:], 
                                        ob['gt_path'], 
                                        threshold=3.0
                                    )['nDTW']
                                elif self.args.expert_policy == 'spl':
                                    # dist = min([self.env.shortest_distances[scan][vpid][end_vp] for end_vp in ob['gt_end_vps']])
                                    dist = self.env.shortest_distances[(ob['scan'], ob['block'])][vpid][ob['endpoint']] \
                                            + self.env.shortest_distances[(ob['scan'], ob['block'])][cur_vp][vpid]
                                if dist < min_dist:
                                    min_dist = dist
                                    min_idx = j
                        a[i] = min_idx
                        if min_idx == self.args.ignoreid:
                            print('scan %s: all vps are searched in teacher_action_r4r' % (scan))
        return torch.from_numpy(a).cuda()

    def _teacher_action_r4r_vln(
        self, gmaps, obs, vpids, ended, visited_masks=None, imitation_learning=False, t=None, traj=None, base_t=None
    ):
        """R4R is not the shortest path. The goal location can be visited nodes.
        """
        a = np.zeros(len(obs), dtype=np.int64)
        for i, ob in enumerate(obs):
            if ended[i]:                                            # Just ignore this index
                a[i] = self.args.ignoreid
            else:
                if imitation_learning:
                    try:
                        assert ob['viewpoint'] == ob['gt_path'][t[i]]
                        assert ob['viewpoint'] == ob['original_path'][base_t]
                    except:
                        print(ob['block'])
                        print(ob['viewpoint'])
                        print(base_t)
                        print(t[i])
                        print(ob['gt_path'])
                        print(ob['original_path'])
                        print(traj[i]['path'])
                        exit(0)

                    if t[i] == len(ob['gt_path']) - 1:
                        a[i] = 0    # stop
                    else:
                        goal_vp = ob['original_path'][base_t + 1]
                        for j, vpid in enumerate(vpids[i]):
                            if goal_vp == vpid:
                                a[i] = j
                                break
                else:
                    if ob['viewpoint'] == ob['gt_path'][-1]:
                        a[i] = 0    # Stop if arrived 
                    else:
                        scan = ob['scan']
                        cur_vp = ob['viewpoint']
                        min_idx, min_dist = self.args.ignoreid, float('inf')
                        for j, vpid in enumerate(vpids[i]):
                            if j > 0 and ((visited_masks is None) or (not visited_masks[i][j])):
                                if self.args.expert_policy == 'ndtw':
                                    dist = - cal_dtw(
                                        self.env.shortest_distances[scan], 
                                        sum(traj[i]['path'], []) + self.env.shortest_paths[scan][ob['viewpoint']][vpid][1:], 
                                        ob['gt_path'], 
                                        threshold=3.0
                                    )['nDTW']
                                elif self.args.expert_policy == 'spl':
                                    # dist = min([self.env.shortest_distances[scan][vpid][end_vp] for end_vp in ob['gt_end_vps']])
                                    real_vpid = gmaps[i].real_name[vpid] if "seen_node" in vpid else vpid
                                    dist = self.env.shortest_distances[(ob['scan'], ob['block'])][real_vpid][ob['endpoint']] \
                                            + self.env.shortest_distances[(ob['scan'], ob['block'])][cur_vp][real_vpid]
                                if dist < min_dist:
                                    min_dist = dist
                                    min_idx = j
                        a[i] = min_idx
                        if min_idx == self.args.ignoreid:
                            print('scan %s: all vps are searched in teacher_action_r4r_vln' % (scan))

        return torch.from_numpy(a).cuda()

    def _teacher_action_r4r_search(
        self, gmap, ob, vpids, ended, target, idx, visited_masks=None, imitation_learning=False, t=None, traj=None
    ):
        """R4R is not the shortest path. The goal location can be visited nodes.
        """
        a = np.zeros(1, dtype=np.int64)
        if ended:                                            # Just ignore this index
            a[0] = self.args.ignoreid
        else:
            if imitation_learning:
                assert ob['viewpoint'] == ob['gt_path'][t]
                if t == len(ob['gt_path']) - 1:
                    a[0] = 0    # stop
                else:
                    goal_vp = ob['gt_path'][t + 1]
                    for j, vpid in enumerate(vpids):
                        if goal_vp == vpid:
                            a[0] = j
                            break
            else:
                if ob['viewpoint'] == target:
                    a[0] = 0    # Stop if arrived 
                else:
                    scan = ob['scan']
                    cur_vp = ob['viewpoint']
                    min_idx, min_dist = self.args.ignoreid, float('inf')
                    for j, vpid in enumerate(vpids):
                        if gmap.graph.distance(ob['viewpoint'], vpid) >= 1e7:
                            continue
                        if j > 0 and ((visited_masks is None) or (not visited_masks[0][j])):
                            
                            # dist = min([self.env.shortest_distances[scan][vpid][end_vp] for end_vp in ob['gt_end_vps']])
                            dist = self.env.shortest_distances[(ob['scan'], ob['block'])][vpid][target] \
                                    + self.env.shortest_distances[(ob['scan'], ob['block'])][cur_vp][vpid]
                            if dist < min_dist:
                                min_dist = dist
                                min_idx = j
                    a[0] = min_idx
                    if min_idx == self.args.ignoreid:
                        print('scan %s: all vps are searched in teacher_action_r4r_search' % (scan))
        return torch.from_numpy(a).cuda()

    def make_equiv_action(self, a_t, gmaps, obs, traj=None):
        """
        Interface between Panoramic view and Egocentric view
        It will convert the action panoramic view action a_t to equivalent egocentric view actions for the simulator
        """
        for i, ob in enumerate(obs):
            action = a_t[i]
            if action is not None:            # None is the <stop> action
                traj[i]['path'].append(gmaps[i].graph.path(ob['viewpoint'], action))
                if len(traj[i]['path'][-1]) == 1:
                    prev_vp = traj[i]['path'][-2][-1]
                else:
                    prev_vp = traj[i]['path'][-1][-2]
                viewidx = self.scanvp_cands['%s_%s'%(ob['scan'], prev_vp)][action]
                heading = (viewidx % 12) * math.radians(30)
                elevation = (viewidx // 12 - 1) * math.radians(30)
                self.env.env.sims[i].newEpisode([ob['scan']], [action], [heading], [elevation])

    def make_equiv_action_vln(self, a_t, gmaps, obs, steps, ml_loss, just_ended, traj=None, train_ml=None):
        """
        Interface between Panoramic view and Egocentric view
        It will convert the action panoramic view action a_t to equivalent egocentric view actions for the simulator
        """
        for i, ob in enumerate(obs):
            action = a_t[i]
            if action is not None:            # None is the <stop> action             
                if gmaps[i].graph.distance(ob['viewpoint'], action) < 1e7:
                    traj[i]['path'].append(gmaps[i].graph.path(ob['viewpoint'], action))
                    if len(traj[i]['path'][-1]) == 1:
                        prev_vp = traj[i]['path'][-2][-1]
                    else:
                        prev_vp = traj[i]['path'][-1][-2] 
                    viewidx = self.scanvp_cands['%s_%s'%(ob['scan'], prev_vp)][action]
                    heading = (viewidx % 12) * math.radians(30)
                    elevation = (viewidx // 12 - 1) * math.radians(30)
                    self.env.env.sims[i].newEpisode([ob['scan']], [action], [heading], [elevation])
                else:
                    search_loss, step = self.search_rollout(action, gmaps[i].real_name[action], gmaps[i], ob, traj[i], steps[i], i, train_ml)
                    steps[i] = step
                    ml_loss += search_loss
                    if steps[i] == self.args.max_action_len - 1:
                        a_t[i] = None
                        just_ended[i] = True

    def make_equiv_action_search(self, idx, a_t, gmap, ob, traj=None):
        """
        Interface between Panoramic view and Egocentric view
        It will convert the action panoramic view action a_t to equivalent egocentric view actions for the simulator
        """
        action = a_t[0]
        if action is not None:            # None is the <stop> action
            traj['path'].append(gmap.graph.path(ob['viewpoint'], action))
            if len(traj['path'][-1]) == 1:
                prev_vp = traj['path'][-2][-1]
            else:
                prev_vp = traj['path'][-1][-2]

            viewidx = self.scanvp_cands['%s_%s'%(ob['scan'], prev_vp)][action]

            heading = (viewidx % 12) * math.radians(30)
            elevation = (viewidx // 12 - 1) * math.radians(30)
            self.env.env.sims[idx].newEpisode([ob['scan']], [action], [heading], [elevation])

    def _update_scanvp_cands(self, obs):
        for ob in obs:
            scan = ob['scan']
            vp = ob['viewpoint']
            scanvp = '%s_%s' % (scan, vp)
            self.scanvp_cands.setdefault(scanvp, {})
            for cand in ob['candidate']:
                self.scanvp_cands[scanvp].setdefault(cand['viewpointId'], {})
                self.scanvp_cands[scanvp][cand['viewpointId']] = cand['pointId']

    def _update_scanvp_cands_search(self, ob):
        scan = ob['scan']
        vp = ob['viewpoint']
        scanvp = '%s_%s' % (scan, vp)
        self.scanvp_cands.setdefault(scanvp, {})
        for cand in ob['candidate']:
            self.scanvp_cands[scanvp].setdefault(cand['viewpointId'], {})
            self.scanvp_cands[scanvp][cand['viewpointId']] = cand['pointId']

    def _stop_action(self, ob, vpids, visited_masks=None):
        a = torch.zeros(1, dtype=torch.int64, device='cuda')
        min_idx, min_dist = self.args.ignoreid, float('inf')
        for i, vpid in enumerate(vpids):
            if i > 0 and visited_masks[i]:
                dist = self.env.shortest_distances[(ob['scan'], ob['block'])][vpid][ob['endpoint']]
                if dist < min_dist:
                    min_dist = dist
                    min_idx = i
        a[0] = min_idx
        return a  
    
    def _nav_target_variable_search(self, ob, gmap, target, search_start_vp):
        target_img_embeds = gmap.get_node_embed(target)
        cur_target_pos_fts = gmap.get_pos_fts(
            ob['viewpoint'], [target], ob['heading'], ob['elevation']
        )
        
        cur_start_pos_fts = gmap.get_pos_fts(
            ob['viewpoint'], [search_start_vp], 
            ob['heading'], ob['elevation']
        ) 
        target_pos_fts = np.zeros((1, 14), dtype=np.float32)
        target_pos_fts[:, :7] = cur_start_pos_fts
        target_pos_fts[:, 7:] = cur_target_pos_fts
        target_pos_fts = torch.from_numpy(target_pos_fts).unsqueeze(0).cuda()

        return {
            'target_img_embeds': target_img_embeds,
            'target_pos_fts': target_pos_fts,
        }

    def search_rollout(self, target, real_target, gmap, ob, traj, start_step, idx, train_ml):

        ended = False

        step = start_step
        search_loss = 0
        search_start_vp = ob['viewpoint']
        while True:
            if not ended and step != start_step:
                gmap.node_step_ids[ob['viewpoint']] = step + 1

            # graph representation
            pano_inputs = self._panorama_feature_variable_search(ob)
            pano_embeds, pano_masks = self.vln_bert('panorama', pano_inputs)

            avg_pano_embeds = torch.sum(pano_embeds * pano_masks.unsqueeze(2), 1) / \
                torch.sum(pano_masks, 1, keepdim=True)

            if not ended and step != start_step:
                # update visited node
                i_vp = ob['viewpoint']
                gmap.update_node_embed(i_vp, avg_pano_embeds[0], rewrite=True)
                # update unvisited nodes
                for j, i_cand_vp in enumerate(pano_inputs['cand_vpids']):
                    if not gmap.graph.visited(i_cand_vp):
                        gmap.update_node_embed(i_cand_vp, pano_embeds[0, j])
                for j, i_seen_vp in enumerate(pano_inputs['seen_vpids']):
                    if not gmap.graph.visited(i_seen_vp):
                        gmap.update_node_embed(i_seen_vp, pano_embeds[0, len(pano_inputs['cand_vpids'])])

            # navigation policy
            nav_inputs = self._nav_gmap_variable_search(ob, gmap)
            nav_inputs.update(
                self._nav_vp_variable_search(ob, gmap, 
                                            pano_embeds, pano_inputs['cand_vpids'], 
                                            pano_inputs['view_lens'], pano_inputs['nav_types'],))

            nav_inputs.update(
                self._nav_target_variable_search(
                    ob, gmap, target, search_start_vp
                )
            )
            
            nav_outs = self.vln_bert('search', nav_inputs)

            if self.args.fusion == 'local':
                nav_logits = nav_outs['local_logits']
                nav_vpids = nav_inputs['vp_cand_vpids']
            elif self.args.fusion == 'global':
                nav_logits = nav_outs['global_logits']
                nav_vpids = nav_inputs['gmap_vpids']
            else:
                nav_logits = nav_outs['fused_logits']
                nav_vpids = nav_inputs['gmap_vpids']
            
            nav_logits.masked_fill_(nav_inputs['seen_mask'].logical_not(), -float('inf'))
            nav_probs = torch.softmax(nav_logits, 1)
            
            # update graph
            if not ended:
                i_vp = ob['viewpoint']
                gmap.node_stop_scores[i_vp] = {
                    'stop': nav_probs[0, 0].data.item(),
                }
            
            if train_ml is not None:
                nav_targets = self._teacher_action_r4r_search(
                    gmap, ob, nav_vpids, ended, real_target, idx,
                    visited_masks=nav_inputs['gmap_visited_masks'] if self.args.fusion != 'local' else None,
                    imitation_learning=(self.feedback=='teacher'), t=step, traj=traj
                )
                search_loss += self.criterion(nav_logits, nav_targets)


            # Determinate the next navigation viewpoint
            if self.feedback == 'teacher':
                a_t = nav_targets                 # teacher forcing
            elif self.feedback == 'argmax':
                _, a_t = nav_logits.max(1)        # student forcing - argmax
                a_t = a_t.detach() 
            elif self.feedback == 'sample':
                c = torch.distributions.Categorical(nav_probs)
                self.logs['entropy'].append(c.entropy().sum().item())            # For log
                a_t = c.sample().detach() 
            elif self.feedback == 'expl_sample':
                _, a_t = nav_probs.max(1)
                rand_explores = np.random.rand(1, ) > self.args.expl_max_ratio  # hyper-param
                if self.args.fusion == 'local':
                    cpu_nav_masks = nav_inputs['vp_nav_masks'].data.cpu().numpy()
                else:
                    cpu_nav_masks = (nav_inputs['gmap_masks'] * nav_inputs['gmap_visited_masks'].logical_not()).data.cpu().numpy()
                for i in range(1):
                    if rand_explores[i]:
                        cand_a_t = np.arange(len(cpu_nav_masks[i]))[cpu_nav_masks[i]]
                        a_t[i] = np.random.choice(cand_a_t)
            else:
                print(self.feedback)
                sys.exit('Invalid feedback option')

            if self.feedback == 'teacher' or self.feedback == 'sample': # in training
                a_t_stop = ob['viewpoint'] == real_target
            else:
                a_t_stop = (a_t == 0) or (gmap.graph.visited(target))

            # Prepare environment action
            cpu_a_t = []  
            if a_t_stop or nav_inputs['no_vp_left'] or (step == self.args.max_action_len - 1):
                cpu_a_t.append(None)
            else:
                cpu_a_t.append(nav_vpids[a_t[0]])   

            # Make action and get the new state
            self.make_equiv_action_search(idx, cpu_a_t, gmap, ob, traj)

            # new observation and update graph
            ob = self.env._get_ob(idx)
            self._update_scanvp_cands_search(ob)
            if not ended:
                gmap.update_graph(ob)

            ended = cpu_a_t[0] is None

            # Early exit if all ended
            if ended:
                break

            step += 1
        
        return search_loss, step

    # @profile
    def rollout(self, train_ml=None, train_rl=False, reset=True):
        if reset:  # Reset env
            obs = self.env.reset()
        else:
            obs = self.env._get_obs()
        self._update_scanvp_cands(obs)

        batch_size = len(obs)
        # build graph: keep the start viewpoint
        gmaps = [GraphMap(ob['viewpoint']) for ob in obs]
        for i, ob in enumerate(obs):
            gmaps[i].update_graph(ob)

        # Record the navigation path
        traj = [{
            'instr_id': ob['instr_id'],
            'path': [[ob['viewpoint']]],
            'details': {},
        } for ob in obs]

        # Language input: txt_ids, txt_masks
        language_inputs = self._language_variable(obs)
        txt_embeds = self.vln_bert('language', language_inputs)
    
        # Initialization the tracking state
        ended = np.array([False] * batch_size)
        just_ended = np.array([False] * batch_size)

        # Init the logs
        masks = []
        entropys = []
        ml_loss = 0.     
        steps = [0] * batch_size
        base_t = 0

        while True:
            for i, gmap in enumerate(gmaps):
                if not ended[i]:
                    gmap.node_step_ids[obs[i]['viewpoint']] = steps[i] + 1

            # graph representation
            pano_inputs = self._panorama_feature_variable(obs)
            pano_embeds, pano_masks = self.vln_bert('panorama', pano_inputs)

            avg_pano_embeds = torch.sum(pano_embeds * pano_masks.unsqueeze(2), 1) / \
                              torch.sum(pano_masks, 1, keepdim=True)

            for i, gmap in enumerate(gmaps):
                if not ended[i]:
                    # update visited node
                    i_vp = obs[i]['viewpoint']
                    gmap.update_node_embed(i_vp, avg_pano_embeds[i], rewrite=True)
                    # update unvisited nodes
                    for j, i_cand_vp in enumerate(pano_inputs['cand_vpids'][i]):
                        if not gmap.graph.visited(i_cand_vp):
                            gmap.update_node_embed(i_cand_vp, pano_embeds[i, j])
                    for j, i_seen_vp in enumerate(pano_inputs['seen_vpids'][i]):
                        if not gmap.graph.visited(i_seen_vp):
                            gmap.update_node_embed(i_seen_vp, pano_embeds[i, len(pano_inputs['cand_vpids'][i])])

            # navigation policy
            nav_inputs = self._nav_gmap_variable(obs, gmaps)
            nav_inputs.update(
                self._nav_vp_variable(
                    obs, gmaps, pano_embeds, pano_inputs['cand_vpids'], 
                    pano_inputs['view_lens'], pano_inputs['nav_types'],
                )
            )
            nav_inputs.update({
                'txt_embeds': txt_embeds,
                'txt_masks': language_inputs['txt_masks'],
            })
            nav_outs = self.vln_bert('navigation', nav_inputs)

            if self.args.fusion == 'local':
                nav_logits = nav_outs['local_logits']
                nav_vpids = nav_inputs['vp_cand_vpids']
            elif self.args.fusion == 'global':
                nav_logits = nav_outs['global_logits']
                nav_vpids = nav_inputs['gmap_vpids']
            else:
                nav_logits = nav_outs['fused_logits']
                nav_vpids = nav_inputs['gmap_vpids']
            
            if not self.args.search_target:
                nav_logits.masked_fill_(nav_inputs['seen_mask'].logical_not(), -float('inf'))
            nav_probs = torch.softmax(nav_logits, 1)
            
            # update graph
            for i, gmap in enumerate(gmaps):
                if not ended[i]:
                    i_vp = obs[i]['viewpoint']
                    gmap.node_stop_scores[i_vp] = {
                        'stop': nav_probs[i, 0].data.item(),
                    }
            
            if train_ml is not None:
                if self.args.search_target:
                    if self.args.dataset == 'r2r':
                        nav_targets = self._teacher_action_r4r_vln(
                            gmaps, obs, nav_vpids, ended, 
                            visited_masks=nav_inputs['gmap_visited_masks'] if self.args.fusion != 'local' else None,
                            imitation_learning=(self.feedback=='teacher'), t=steps, traj=traj, base_t=base_t
                        )
                    elif self.args.dataset == 'r4r':
                        nav_targets = self._teacher_action_r4r_vln(
                            gmaps, obs, nav_vpids, ended, 
                            visited_masks=nav_inputs['gmap_visited_masks'] if self.args.fusion != 'local' else None,
                            imitation_learning=(self.feedback=='teacher'), t=steps, traj=traj, base_t=base_t
                        )
                else:
                    if self.args.dataset == 'r2r':
                        nav_targets = self._teacher_action_r4r(
                            gmaps, obs, nav_vpids, ended, 
                            visited_masks=nav_inputs['gmap_visited_masks'] if self.args.fusion != 'local' else None,
                            imitation_learning=(self.feedback=='teacher'), t=base_t, traj=traj
                        )
                    elif self.args.dataset == 'r4r':
                        nav_targets = self._teacher_action_r4r(
                            gmaps, obs, nav_vpids, ended, 
                            visited_masks=nav_inputs['gmap_visited_masks'] if self.args.fusion != 'local' else None,
                            imitation_learning=(self.feedback=='teacher'), t=base_t, traj=traj
                        )
                # print(t, nav_logits, nav_targets)
                ml_loss += self.criterion(nav_logits, nav_targets)
                # print(t, 'ml_loss', ml_loss.item(), self.criterion(nav_logits, nav_targets).item())

            # Determinate the next navigation viewpoint
            if self.feedback == 'teacher':
                a_t = nav_targets                 # teacher forcing
            elif self.feedback == 'argmax':
                _, a_t = nav_logits.max(1)        # student forcing - argmax
                a_t = a_t.detach() 
            elif self.feedback == 'sample':
                c = torch.distributions.Categorical(nav_probs)
                self.logs['entropy'].append(c.entropy().sum().item())            # For log
                entropys.append(c.entropy())                                     # For optimization
                a_t = c.sample().detach() 
            elif self.feedback == 'expl_sample':
                _, a_t = nav_probs.max(1)
                rand_explores = np.random.rand(batch_size, ) > self.args.expl_max_ratio  # hyper-param
                if self.args.fusion == 'local':
                    cpu_nav_masks = nav_inputs['vp_nav_masks'].data.cpu().numpy()
                else:
                    cpu_nav_masks = (nav_inputs['gmap_masks'] * nav_inputs['gmap_visited_masks'].logical_not()).data.cpu().numpy()
                for i in range(batch_size):
                    if rand_explores[i]:
                        cand_a_t = np.arange(len(cpu_nav_masks[i]))[cpu_nav_masks[i]]
                        a_t[i] = np.random.choice(cand_a_t)
            else:
                print(self.feedback)
                sys.exit('Invalid feedback option')

            # Determine stop actions
            if self.feedback == 'teacher' or self.feedback == 'sample': # in training
                # a_t_stop = [ob['viewpoint'] in ob['gt_end_vps'] for ob in obs]
                a_t_stop = [ob['viewpoint'] == ob['gt_path'][-1] for ob in obs]
            else:
                a_t_stop = a_t == 0

            # Prepare environment action
            cpu_a_t = []  
            for i in range(batch_size):
                if a_t_stop[i] or ended[i] or nav_inputs['no_vp_left'][i] or (steps[i] == self.args.max_action_len - 1):
                    cpu_a_t.append(None)
                    just_ended[i] = True
                else:
                    cpu_a_t.append(nav_vpids[i][a_t[i]])   

            # Make action and get the new state
            if self.args.search_target:
                self.make_equiv_action_vln(cpu_a_t, gmaps, obs, steps, ml_loss, just_ended, traj, train_ml)
            else:
                self.make_equiv_action(cpu_a_t, gmaps, obs, traj)

            # Determinate the stop viewpoint
            if self.args.seen_stop:
                for i in range(batch_size):
                    if (not ended[i]) and just_ended[i]:
                        stop_logits = nav_outs['visited_global_logits'][i:i+1]
                        if train_ml is not None:
                            stop_targets = self._stop_action(
                                obs[i], nav_vpids[i], 
                                visited_masks=nav_inputs['gmap_visited_masks'][i]
                            )
                            ml_loss += self.criterion(stop_logits, stop_targets)
                                
                        if self.feedback == 'teacher':
                            assert obs[i]['viewpoint'] ==  obs[i]['gt_path'][-1]
                        else:
                            _, s_t = stop_logits.max(1)
                            s_t = s_t.detach().data.item()
                            stop_node = nav_vpids[i][s_t]
                            if stop_node != obs[i]['viewpoint']:
                                traj[i]['path'].append(gmaps[i].graph.path(obs[i]['viewpoint'], stop_node))
            else:
                for i in range(batch_size):
                    if (not ended[i]) and just_ended[i]:
                        stop_node, stop_score = None, {'stop': -float('inf')}
                        for k, v in gmaps[i].node_stop_scores.items():
                            if v['stop'] > stop_score['stop']:
                                stop_score = v
                                stop_node = k
                        if stop_node is not None and obs[i]['viewpoint'] != stop_node:
                            traj[i]['path'].append(gmaps[i].graph.path(obs[i]['viewpoint'], stop_node))
                        if self.args.detailed_output:
                            for k, v in gmaps[i].node_stop_scores.items():
                                traj[i]['details'][k] = {
                                    'stop_prob': float(v['stop']),
                                }
                                
            # new observation and update graph
            obs = self.env._get_obs()
            self._update_scanvp_cands(obs)
            for i, ob in enumerate(obs):
                if not ended[i]:
                    gmaps[i].update_graph(ob)

            ended[:] = np.logical_or(ended, np.array([x is None for x in cpu_a_t]))

            # Early exit if all ended
            if ended.all():
                break

            steps = [x+1 for x in steps]
            base_t += 1

        if train_ml is not None:
            ml_loss = ml_loss * train_ml / batch_size
            self.loss += ml_loss
            self.logs['IL_loss'].append(ml_loss.item())

        # # test whether containing blocks
        # for i in range(batch_size):
        #     flat_traj = [item for sublist in traj[i]['path'] for item in sublist]
        #     for j in range(len(flat_traj)-1):
        #         edge = (flat_traj[j], flat_traj[j+1])
        #         block = obs[i]['block']
        #         if block:
        #             if edge in block or edge[::-1] in block:
        #                 print(f"Error: {traj[i]['instr_id']} contains block edge {edge}")
        #                 print("traj:", flat_traj)
        #                 print("block:", block)
        #                 exit(0)
        
        return traj
