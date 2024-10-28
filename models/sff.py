import torch
import torch.nn as nn
from timm.models.layers import  trunc_normal_
import math



class SFF(nn.Module):
    def __init__(self, dim):
        super().__init__()
