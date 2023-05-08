from __future__ import annotations

import torch
import torch.nn as nn
import triton
import triton.language as tl
import warnings
from typing import Sequence

from pyjuice.nodes import ProdNodes
from .layer import Layer
from .backend.node_partition import partition_nodes_by_n_edges
from .backend.index_set import batched_index_set, batched_index_cum

# Try to enable tensor cores
torch.set_float32_matmul_precision('high')


class ProdLayer(Layer, nn.Module):

    def __init__(self, nodes: Sequence[ProdNodes], max_num_groups: int = 1) -> None:
        Layer.__init__(self)
        nn.Module.__init__(self)

        assert len(nodes) > 0, "No input node."

        self.nodes = nodes

        max_n_chs = 0
        layer_num_nodes = 0
        cum_nodes = 1 # id 0 is reserved for the dummy node
        for ns in self.nodes:
            if ns.num_child_regions > max_n_chs:
                max_n_chs = ns.num_child_regions
            ns._output_ind_range = (cum_nodes, cum_nodes + ns.num_nodes)
            cum_nodes += ns.num_nodes
            layer_num_nodes += ns.num_nodes

        self.num_nodes = layer_num_nodes

        ## Initialize forward pass ##

        cids = torch.zeros([self.num_nodes, max_n_chs], dtype = torch.long) # cids[i,:] are the child node ids for node i
        n_chs = torch.zeros([self.num_nodes], dtype = torch.long) # Number of children for each node
        node_start = 0
        for ns in self.nodes:
            node_end = node_start + ns.num_nodes
            for i, c in enumerate(ns.chs):
                cids[node_start:node_end, i] = ns.edge_ids[:,i] + c._output_ind_range[0]
                
            n_chs[node_start:node_end] = ns.num_child_regions

            node_start = node_end

        # Find a good strategy to partition the nodes into groups according to their number of children 
        # to minimize total computation cost
        ch_group_sizes = partition_nodes_by_n_edges(n_chs, max_num_groups = max_num_groups)

        grouped_nids = []
        grouped_cids = []
        min_n_chs = 0
        for max_n_chs in ch_group_sizes:
            filter = (n_chs >= min_n_chs) & (n_chs <= max_n_chs)
            curr_nids = (torch.where(filter)[0] + 1).clone()
            curr_cids = cids[filter,:max_n_chs].contiguous()

            grouped_nids.append(curr_nids)
            grouped_cids.append(curr_cids)

            min_n_chs = max_n_chs + 1

        self.num_forward_groups = ch_group_sizes.shape[0]
        self.grouped_nids = nn.ParameterList([nn.Parameter(tensor, requires_grad = False) for tensor in grouped_nids])
        self.grouped_cids = nn.ParameterList([nn.Parameter(tensor, requires_grad = False) for tensor in grouped_cids])

        ## Initialize backward pass ##

        u_cids, par_counts = torch.unique(cids, sorted = True, return_counts = True)

        if u_cids[0] != 0:
            u_cids = torch.cat(
                (torch.zeros([1], dtype = torch.long), u_cids),
                dim = 0
            )
            par_counts = torch.cat(
                (torch.zeros([1], dtype = torch.long), par_counts),
                dim = 0
            )

        max_n_pars = torch.max(par_counts[1:])
        parids = torch.zeros([u_cids.size(0), max_n_pars], dtype = torch.long)
        for idx in range(u_cids.size(0)):
            cid = u_cids[idx]
            if cid == 0:
                continue # Skip the dummy node
            b_nid = torch.where(cids == cid)[0]
            parids[idx,:b_nid.size(0)] = b_nid + 1 # 1 for the dummy node

        # Find a good strategy to partition the child nodes into groups according to their number of parents 
        # to minimize total computation cost
        par_counts = par_counts[1:] # Strip away the dummy node. We will never use it in the following
        par_group_sizes = partition_nodes_by_n_edges(par_counts, max_num_groups = max_num_groups)

        grouped_parids = []
        grouped_u_cids = []
        min_n_pars = 0
        for max_n_pars in par_group_sizes:
            filter = (par_counts >= min_n_pars) & (par_counts <= max_n_pars)
            filtered_idxs = torch.where(filter)[0]
            curr_parids = parids[filtered_idxs+1,:max_n_pars].contiguous()
            curr_u_cids = u_cids[filtered_idxs+1].contiguous()

            grouped_parids.append(curr_parids)
            grouped_u_cids.append(curr_u_cids)

            min_n_pars = max_n_pars + 1

        self.num_backward_groups = par_group_sizes.shape[0]
        self.grouped_parids = nn.ParameterList([nn.Parameter(tensor, requires_grad = False) for tensor in grouped_parids])
        self.grouped_u_cids = nn.ParameterList([nn.Parameter(tensor, requires_grad = False) for tensor in grouped_u_cids])

    def forward(self, node_mars: torch.Tensor, element_mars: torch.Tensor, scratch: torch.Tensor, skip_logsumexp: bool = False):
        """
        node_mars: [num_nodes, B]
        element_mars: [max_num_els, B]
        """
        if skip_logsumexp:
            self._dense_forward_pass_nolog(node_mars, element_mars)
        else:
            self._dense_forward_pass(node_mars, element_mars, scratch)

    def backward(self, node_flows: torch.Tensor, element_flows: torch.Tensor, scratch: torch.Tensor, skip_logsumexp: bool = False):
        """
        node_flows: [num_nodes, B]
        element_flows: [max_num_els, B]
        """
        if skip_logsumexp:
            self._dense_backward_pass_nolog(node_flows, element_flows)
        else:
            self._dense_backward_pass(node_flows, element_flows, scratch)

    @staticmethod
    @torch.compile(mode = "reduce-overhead")
    def _forward_fn(scratch, node_mars, cids):
        scratch[:] = (node_mars[cids].sum(dim = 1)).reshape(-1)

        return None

    def _dense_forward_pass(self, node_mars: torch.Tensor, element_mars: torch.Tensor, scratch: torch.Tensor):
        
        batch_size = node_mars.size(1)
        
        for group_id in range(self.num_forward_groups):
            nids = self.grouped_nids[group_id]
            cids = self.grouped_cids[group_id]

            self._forward_fn(scratch[:nids.numel() * batch_size], node_mars, cids)
            batched_index_set(element_mars, nids, scratch)

        return None

    @torch.compile(mode = "reduce-overhead")
    def _dense_forward_pass_legacy(self, node_mars: torch.Tensor, element_mars: torch.Tensor):
        for group_id in range(self.num_forward_groups):
            nids = self.grouped_nids[group_id]
            cids = self.grouped_cids[group_id]

            element_mars[nids,:] = node_mars[cids].sum(dim = 1)

        return None

    @torch.compile(mode = "reduce-overhead")
    def _dense_forward_pass_nolog(self, node_mars: torch.Tensor, element_mars: torch.Tensor):
        for group_id in range(self.num_forward_groups):
            nids = self.grouped_nids[group_id]
            cids = self.grouped_cids[group_id]

            element_mars[nids,:] = node_mars[cids].prod(dim = 1)

        return None
    
    @torch.compile(mode = "reduce-overhead")
    def _dense_backward_pass_nolog(self, node_flows: torch.Tensor, element_flows: torch.Tensor):
        for group_id in range(self.num_backward_groups):
            parids = self.grouped_parids[group_id]
            u_cids = self.grouped_u_cids[group_id]

            node_flows[u_cids] += element_flows[parids].prod(dim = 1)
        
        return None

    @staticmethod
    @torch.compile(mode = "reduce-overhead")
    def _backward_fn(scratch, element_flows, parids):
        scratch[:] = (element_flows[parids].sum(dim = 1)).reshape(-1)

        return None

    def _dense_backward_pass(self, node_flows: torch.Tensor, element_flows: torch.Tensor, scratch: torch.Tensor):
        
        batch_size = node_flows.size(1)
        
        for group_id in range(self.num_backward_groups):
            parids = self.grouped_parids[group_id]
            u_cids = self.grouped_u_cids[group_id]

            self._backward_fn(scratch[:u_cids.numel() * batch_size], element_flows, parids)
            batched_index_cum(node_flows, u_cids, scratch)
        
        return None

    @torch.compile(mode = "reduce-overhead")
    def _dense_backward_pass_legacy(self, node_flows: torch.Tensor, element_flows: torch.Tensor):
        for group_id in range(self.num_backward_groups):
            parids = self.grouped_parids[group_id]
            u_cids = self.grouped_u_cids[group_id]

            node_flows[u_cids] += element_flows[parids].sum(dim = 1)
        
        return None