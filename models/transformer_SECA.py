import copy
from typing import Optional, List

import torch
import torch.nn.functional as F
from torch import nn, Tensor

from .attention_layer import GaussianMultiheadAttention
from models.latt import LocalAttention

class Transformer(nn.Module):

    def __init__(self, config, d_model=512, nhead=8, num_encoder_layers=6,
                 num_decoder_layers=6, dim_feedforward=2048, dropout=0.1,
                 activation="relu", normalize_before=False,
                 return_intermediate_dec=False, smooth=8, dynamic_scale=True):
        super().__init__()

        encoder_layer = TransformerEncoderLayer(d_model, nhead, dim_feedforward,
                                                dropout, activation, normalize_before)
        encoder_norm = nn.LayerNorm(d_model) if normalize_before else None
        self.encoder = TransformerEncoder(encoder_layer, num_encoder_layers, encoder_norm)
        self.embeddings = DecoderEmbeddings(config)

        decoder_layers = []
        for layer_index in range(num_decoder_layers):
            decoder_layer = TransformerDecoderLayer(dynamic_scale, smooth, layer_index,
                                                    d_model, nhead, dim_feedforward, dropout,
                                                    activation, normalize_before)
            decoder_layers.append(decoder_layer)
        decoder_norm = nn.LayerNorm(d_model)
        self.decoder = TransformerDecoder(decoder_layers, num_decoder_layers, decoder_norm,
                                          return_intermediate=return_intermediate_dec)

        self._reset_parameters()
        if dynamic_scale in ["type2", "type3", "type4"]:
            for layer_index in range(num_decoder_layers):
                nn.init.zeros_(self.decoder.layers[layer_index].point3.weight)
                with torch.no_grad():
                    nn.init.ones_(self.decoder.layers[layer_index].point3.bias)

        self.d_model = d_model
        self.nhead = nhead

    def _reset_parameters(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def forward(self, src, mask, pos_embed, tgt, tgt_mask, h_w):
        bs, c, h, w = src.shape
        
        
        grid_y, grid_x = torch.meshgrid(torch.arange(0, h), torch.arange(0, w))
        grid = torch.stack((grid_x, grid_y), 2).float().to(src.device)
        grid = grid.reshape(-1, 2).unsqueeze(1).repeat(1, bs * self.nhead, 1)

        src = src.flatten(2).permute(2, 0, 1)
        pos_embed = pos_embed.flatten(2).permute(2, 0, 1)

        tgt = self.embeddings(tgt).permute(1, 0, 2)
        query_embed = self.embeddings.position_embeddings.weight.unsqueeze(1)
        query_embed = query_embed.repeat(1, bs, 1)

        mask = mask.flatten(1)
        
        
        memory = self.encoder(src, src_key_padding_mask=mask, pos=pos_embed)#torch.Size([256, 64, 256])
        
        
        hs = self.decoder(grid, h_w, tgt, memory, memory_key_padding_mask=mask, tgt_key_padding_mask=tgt_mask,
                                  pos=pos_embed, query_pos=query_embed,
                                  tgt_mask=generate_square_subsequent_mask(len(tgt)).to(tgt.device))#grid=torch.Size([256, 256, 2])h_w=torch.Size([1, 64, 2])
        return hs


class TransformerEncoder(nn.Module):

    def __init__(self, encoder_layer, num_layers, norm=None):
        super().__init__()
        self.layers = _get_clones(encoder_layer, num_layers)
        self.num_layers = num_layers
        self.norm = norm

    def forward(self, src,
                mask: Optional[Tensor] = None,
                src_key_padding_mask: Optional[Tensor] = None,
                pos: Optional[Tensor] = None):
        output = src

        for layer in self.layers:
            output = layer(output, src_mask=mask,
                           src_key_padding_mask=src_key_padding_mask, pos=pos)

        if self.norm is not None:
            output = self.norm(output)

        return output


class TransformerDecoder(nn.Module):

    def __init__(self, decoder_layer, num_layers, norm=None, return_intermediate=False):
        super().__init__()
        self.layers = nn.ModuleList(decoder_layer)
        self.num_layers = num_layers
        self.norm = norm
        self.return_intermediate = return_intermediate

    def forward(self, grid, h_w, tgt, memory,
                tgt_mask: Optional[Tensor] = None,
                memory_mask: Optional[Tensor] = None,
                tgt_key_padding_mask: Optional[Tensor] = None,
                memory_key_padding_mask: Optional[Tensor] = None,
                pos: Optional[Tensor] = None,
                query_pos: Optional[Tensor] = None):
        output = tgt

        intermediate = []

        points = []
        point_sigmoid_ref = None
        for layer in self.layers:
            output, point, point_sigmoid_ref = layer(
                grid, h_w, output, memory, tgt_mask=tgt_mask,
                memory_mask=memory_mask, tgt_key_padding_mask=tgt_key_padding_mask,
                memory_key_padding_mask=memory_key_padding_mask,
                pos=pos, query_pos=query_pos, point_ref_previous=point_sigmoid_ref
            )
            points.append(point)
            if self.return_intermediate:
                intermediate.append(self.norm(output))

        if self.norm is not None:
            output = self.norm(output)
            if self.return_intermediate:
                intermediate.pop()
                intermediate.append(output)

        if self.return_intermediate:
            return torch.stack(intermediate), points[0]

        return output


class TransformerEncoderLayer(nn.Module):

    def __init__(self, d_model, nhead, dim_feedforward=2048, dropout=0.1,
                 activation="relu", normalize_before=False):
        super().__init__()
        self.self_attn = LocalAttention(d_model, nhead, dropout=dropout)
        self.linear1 = nn.Linear(d_model, dim_feedforward)
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(dim_feedforward, d_model)

        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)
        self.activation = _get_activation_fn(activation)
        self.normalize_before = normalize_before

    def with_pos_embed(self, tensor, pos: Optional[Tensor]):
        return tensor if pos is None else tensor + pos

    def forward_post(self,
                     src,
                     pos: Optional[Tensor] = None):
        q = k = self.with_pos_embed(src, pos)
        src2 = self.self_attn(q, k,shape=(16,16), value=src)
        src = src + self.dropout1(src2)
        src = self.norm1(src)
        src2 = self.linear2(self.dropout(self.activation(self.linear1(src))))
        src = src + self.dropout2(src2)
        src = self.norm2(src)
        return src
 
    def forward_pre(self, src,
                    pos: Optional[Tensor] = None):
        src2 = self.norm1(src)
        q = k = self.with_pos_embed(src2, pos)
        src2 = self.self_attn(q, k,shape=(16,16), value=src2)
        src = src + self.dropout1(src2)
        src2 = self.norm2(src)
        src2 = self.linear2(self.dropout(self.activation(self.linear1(src2))))
        src = src + self.dropout2(src2)
        return src

    def forward(self, src,
                src_mask: Optional[Tensor] = None,
                src_key_padding_mask: Optional[Tensor] = None,
                pos: Optional[Tensor] = None):
        if self.normalize_before:
            return self.forward_pre(src, src_mask, src_key_padding_mask, pos)
        return self.forward_post(src, src_mask, src_key_padding_mask, pos)


class TransformerDecoderLayer(nn.Module):

    def __init__(self, dynamic_scale, smooth, layer_index,
                 d_model, nhead, dim_feedforward=2048, dropout=0.1,
                 activation="relu", normalize_before=False):
        super().__init__()
        self.self_attn = nn.MultiheadAttention(d_model, nhead, dropout=dropout)
        self.multihead_attn = GaussianMultiheadAttention(d_model, nhead, dropout=dropout)
        self.linear1 = nn.Linear(d_model, dim_feedforward)
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(dim_feedforward, d_model)

        self.smooth = smooth
        self.dynamic_scale = dynamic_scale

        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.norm3 = nn.LayerNorm(d_model)
        self.norm4 = nn.LayerNorm(d_model)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)
        self.dropout3 = nn.Dropout(dropout)

        if layer_index == 0:
            self.point1 = MLP(d_model, d_model, 2, 3)
            self.point2 = nn.Linear(d_model, 2*nhead)
        else:
            self.point2 = nn.Linear(d_model, 2*nhead)
        self.layer_index = layer_index
        if self.dynamic_scale == "type2":
            self.point3 = nn.Linear(d_model, nhead)
        elif self.dynamic_scale == "type3":
            self.point3 = nn.Linear(d_model, 2*nhead)
        elif self.dynamic_scale == "type4":
            self.point3 = nn.Linear(d_model, 3*nhead)

        self.activation = _get_activation_fn(activation)
        self.normalize_before = normalize_before

        self.nhead = nhead
        

    def with_pos_embed(self, tensor, pos: Optional[Tensor]):
        return tensor if pos is None else tensor + pos

    def forward_post(self, grid, h_w, tgt, memory,
                     tgt_mask: Optional[Tensor] = None,
                     memory_mask: Optional[Tensor] = None,
                     tgt_key_padding_mask: Optional[Tensor] = None,
                     memory_key_padding_mask: Optional[Tensor] = None,
                     pos: Optional[Tensor] = None,
                     query_pos: Optional[Tensor] = None,
                     point_ref_previous: Optional[Tensor] = None):
        tgt_len = tgt.shape[0]

        out = self.norm4(tgt + query_pos)
        point_sigmoid_offset = self.point2(out)

        q = k = self.with_pos_embed(tgt, query_pos)
        tgt2 = self.self_attn(q, k, value=tgt, attn_mask=tgt_mask,
                              key_padding_mask=tgt_key_padding_mask)[0]
        tgt = tgt + self.dropout1(tgt2)
        tgt = self.norm1(tgt)

        if self.layer_index == 0:
            point_sigmoid_ref_inter = self.point1(out)
            point_sigmoid_ref = point_sigmoid_ref_inter.sigmoid()
            point_sigmoid_ref = (h_w - 0) * point_sigmoid_ref / int((h_w.max().item()/int(grid.max()+1)))
            point_sigmoid_ref = point_sigmoid_ref.repeat(1, 1, self.nhead)
        else:
            point_sigmoid_ref = point_ref_previous

        point = point_sigmoid_ref + point_sigmoid_offset
        point = point.view(tgt_len, -1, 2)
        distance = (point.unsqueeze(1) - grid.unsqueeze(0)).pow(2)

        if self.dynamic_scale == "type1":
            scale = 1
            distance = distance.sum(-1) * scale
        elif self.dynamic_scale == "type2":
            scale = self.point3(out)
            scale = scale * scale
            scale = scale.reshape(tgt_len, -1).unsqueeze(1)
            distance = distance.sum(-1) * scale
        elif self.dynamic_scale == "type3":
            scale = self.point3(out)
            scale = scale * scale
            scale = scale.reshape(tgt_len, -1, 2).unsqueeze(1)
            distance = (distance * scale).sum(-1)
        elif self.dynamic_scale == "type4":
            scale = self.point3(out)
            scale = scale * scale
            scale = scale.reshape(tgt_len, -1, 3).unsqueeze(1)
            distance = torch.cat([distance, torch.prod(distance, dim=-1, keepdim=True)], dim=-1)
            distance = (distance * scale).sum(-1)

        gaussian = -(distance - 0).abs() / self.smooth

        tgt2 = self.multihead_attn(query=self.with_pos_embed(tgt, query_pos),
                                   key=self.with_pos_embed(memory, pos),
                                   value=memory, attn_mask=memory_mask,
                                   key_padding_mask=memory_key_padding_mask,
                                   gaussian=[gaussian])[0]
        tgt = tgt + self.dropout2(tgt2)
        tgt = self.norm2(tgt)
        tgt2 = self.linear2(self.dropout(self.activation(self.linear1(tgt))))
        tgt = tgt + self.dropout3(tgt2)
        tgt = self.norm3(tgt)
        if self.layer_index == 0:
            return tgt, point_sigmoid_ref_inter, point_sigmoid_ref
        else:
            return tgt, None, point_sigmoid_ref

    def forward_pre(self, grid, h_w, tgt, memory,
                    tgt_mask: Optional[Tensor] = None,
                    memory_mask: Optional[Tensor] = None,
                    tgt_key_padding_mask: Optional[Tensor] = None,
                    memory_key_padding_mask: Optional[Tensor] = None,
                    pos: Optional[Tensor] = None,
                    query_pos: Optional[Tensor] = None,
                    point_ref_previous: Optional[Tensor] = None):

        tgt_len = tgt.shape[0]
        out = self.norm4(tgt + query_pos)
        point_sigmoid_offset = self.point2(out)    

        tgt2 = self.norm1(tgt)
        q = k = self.with_pos_embed(tgt2, query_pos)
        tgt2 = self.self_attn(q, k, value=tgt2, attn_mask=tgt_mask,
                              key_padding_mask=tgt_key_padding_mask)[0]
        tgt = tgt + self.dropout1(tgt2)

        if self.layer_index == 0:
            point_sigmoid_ref_inter = self.point1(out)
            point_sigmoid_ref = point_sigmoid_ref_inter.sigmoid()
            point_sigmoid_ref = (h_w - 0) * point_sigmoid_ref / int((h_w.max().item()/int(grid.max()+1)))
            point_sigmoid_ref = point_sigmoid_ref.repeat(1, 1, self.nhead)
        else:
            point_sigmoid_ref = point_ref_previous

        point = point_sigmoid_ref + point_sigmoid_offset
        point = point.view(tgt_len, -1, 2)
        distance = (point.unsqueeze(1) - grid.unsqueeze(0)).pow(2)

        if self.dynamic_scale == "type1":
            scale = 1
            distance = distance.sum(-1) * scale
        elif self.dynamic_scale == "type2":
            scale = self.point3(out)
            scale = scale * scale
            scale = scale.reshape(tgt_len, -1).unsqueeze(1)
            distance = distance.sum(-1) * scale
        elif self.dynamic_scale == "type3":
            scale = self.point3(out)
            scale = scale * scale
            scale = scale.reshape(tgt_len, -1, 2).unsqueeze(1)
            distance = (distance * scale).sum(-1)
        elif self.dynamic_scale == "type4":
            scale = self.point3(out)
            scale = scale * scale
            scale = scale.reshape(tgt_len, -1, 3).unsqueeze(1)
            distance = torch.cat([distance, torch.prod(distance, dim=-1, keepdim=True)], dim=-1)
            distance = (distance * scale).sum(-1)

        gaussian = -(distance - 0).abs() / self.smooth

        tgt2 = self.norm2(tgt)
        tgt2 = self.multihead_attn(query=self.with_pos_embed(tgt2, query_pos),
                                   key=self.with_pos_embed(memory, pos),
                                   value=memory, attn_mask=memory_mask,
                                   key_padding_mask=memory_key_padding_mask,
                                   gaussian=[gaussian])[0]
        tgt = tgt + self.dropout2(tgt2)
        tgt2 = self.norm3(tgt)
        tgt2 = self.linear2(self.dropout(self.activation(self.linear1(tgt2))))
        tgt = tgt + self.dropout3(tgt2)
        
        if self.layer_index == 0:
            return tgt, point_sigmoid_ref_inter, point_sigmoid_ref
        else:
            return tgt, None, point_sigmoid_ref

    def forward(self, grid, h_w, tgt, memory,
                tgt_mask: Optional[Tensor] = None,
                memory_mask: Optional[Tensor] = None,
                tgt_key_padding_mask: Optional[Tensor] = None,
                memory_key_padding_mask: Optional[Tensor] = None,
                pos: Optional[Tensor] = None,
                query_pos: Optional[Tensor] = None,
                point_ref_previous: Optional[Tensor] = None):
        if self.normalize_before:
            return self.forward_pre(grid, h_w, tgt, memory, tgt_mask, memory_mask,
                                    tgt_key_padding_mask, memory_key_padding_mask, pos, query_pos,
                                    point_ref_previous)
        return self.forward_post(grid, h_w, tgt, memory, tgt_mask, memory_mask,
                                 tgt_key_padding_mask, memory_key_padding_mask, pos, query_pos,
                                 point_ref_previous)#没走这边


class MLP(nn.Module):
    """ Very simple multi-layer perceptron (also called FFN)"""

    def __init__(self, input_dim, hidden_dim, output_dim, num_layers):
        super().__init__()
        self.num_layers = num_layers
        h = [hidden_dim] * (num_layers - 1)
        self.layers = nn.ModuleList(nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim]))

    def forward(self, x):
        for i, layer in enumerate(self.layers):
            x = F.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
        return x


def _get_clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for i in range(N)])


class DecoderEmbeddings(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.word_embeddings = nn.Embedding(
            config.vocab_size, config.hidden_dim, padding_idx=config.PAD_token_id)
        self.position_embeddings = nn.Embedding(
            config.max_position_embeddings, config.hidden_dim
        )

        self.LayerNorm = torch.nn.LayerNorm(
            config.hidden_dim, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.dropout)

    def forward(self, x):
        input_shape = x.size()
        seq_length = input_shape[1]
        device = x.device

        position_ids = torch.arange(
            seq_length, dtype=torch.long, device=device)
        position_ids = position_ids.unsqueeze(0).expand(input_shape)

        input_embeds = self.word_embeddings(x)
        position_embeds = self.position_embeddings(position_ids)

        embeddings = input_embeds + position_embeds
        embeddings = self.LayerNorm(embeddings)
        embeddings = self.dropout(embeddings)

        return embeddings


def generate_square_subsequent_mask(sz):
    r"""Generate a square mask for the sequence. The masked positions are filled with float('-inf').
        Unmasked positions are filled with float(0.0).
    """
    mask = (torch.triu(torch.ones(sz, sz)) == 1).transpose(0, 1)
    mask = mask.float().masked_fill(mask == 0, float(
        '-inf')).masked_fill(mask == 1, float(0.0))
    return mask


def build_transformer(config):
    return Transformer(
        config,
        d_model=config.hidden_dim,
        dropout=config.dropout,
        nhead=config.nheads,
        dim_feedforward=config.dim_feedforward,
        num_encoder_layers=config.enc_layers,
        num_decoder_layers=config.dec_layers,
        normalize_before=config.pre_norm,
        return_intermediate_dec=False,
        smooth=config.smooth,
        dynamic_scale=config.dynamic_scale,
    )


def _get_activation_fn(activation):
    """Return an activation function given a string"""
    if activation == "relu":
        return F.relu
    if activation == "gelu":
        return F.gelu
    if activation == "glu":
        return F.glu
    raise RuntimeError(F"activation should be relu/gelu, not {activation}.")
