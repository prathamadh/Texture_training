import torch
import torch.nn as nn
import numpy as np
import math
import torch.nn.functional as F



def compute_depth_expectation(prob, depth_values):
    depth_values = depth_values.view(*depth_values.shape, 1, 1)
    depth = torch.sum(prob * depth_values, 1)
    return depth

# def compute_roughness_expectation(prob, depth_values):
#     depth_values = depth_values.view(*depth_values.shape, 1, 1)
#     depth = torch.sum(prob * depth_values, 1)
#     return depth

def interpolate_float32(x, size=None, scale_factor=None, mode='nearest', align_corners=None):
    with torch.autocast(device_type='cuda', dtype=torch.bfloat16, enabled=False):
        return F.interpolate(x.float(), size=size, scale_factor=scale_factor, mode=mode, align_corners=align_corners)

def upflow8(flow, mode='bilinear'):
    new_size = (8 * flow.shape[2], 8 * flow.shape[3])
    return  8 * F.interpolate(flow, size=new_size, mode=mode, align_corners=True)

def upflow4(flow, mode='bilinear'):
    new_size = (4 * flow.shape[2], 4 * flow.shape[3])
    with torch.autocast(device_type='cuda', dtype=torch.bfloat16, enabled=False):
        return  F.interpolate(flow, size=new_size, mode=mode, align_corners=True)

def coords_grid(batch, ht, wd):
    # coords = torch.meshgrid(torch.arange(ht), torch.arange(wd))
    coords = (torch.zeros((ht, wd)), torch.zeros((ht, wd)), torch.zeros((ht, wd)), torch.zeros((ht, wd)), torch.zeros((ht, wd)), torch.zeros((ht, wd)),torch.zeros((ht, wd)),torch.zeros((ht, wd)),torch.zeros((ht, wd)))
    coords = torch.stack(coords[::-1], dim=0).float()
    return coords[None].repeat(batch, 1, 1, 1)

def norm_normalize(norm_out):
    min_kappa = 0.01
    norm_x, norm_y, norm_z, kappa = torch.split(norm_out, 1, dim=1)
    norm = torch.sqrt(norm_x ** 2.0 + norm_y ** 2.0 + norm_z ** 2.0) + 1e-10
    kappa = F.elu(kappa) + 1.0 + min_kappa
    final_out = torch.cat([norm_x / norm, norm_y / norm, norm_z / norm, kappa], dim=1)
    return final_out
def norm_normalize_single_channel(norm_out):
    min_kappa = 0.01
    # Compute the norm and apply transformations to single-channel input
    norm = torch.sqrt(norm_out ** 2.0) + 1e-10
    norm_out = F.elu(norm_out) + 1.0 + min_kappa
    final_out = norm_out / norm
    return final_out
# uncertainty-guided sampling (only used during training)
@torch.no_grad()
def sample_points(init_normal, gt_norm_mask, sampling_ratio, beta):
    device = init_normal.device
    B, _, H, W = init_normal.shape
    N = int(sampling_ratio * H * W)
    beta = beta

    # uncertainty map
    uncertainty_map = -1 * init_normal[:, -1, :, :]  # B, H, W

    # gt_invalid_mask (B, H, W)
    if gt_norm_mask is not None:
        gt_invalid_mask = F.interpolate(gt_norm_mask.float(), size=[H, W], mode='nearest')
        gt_invalid_mask = gt_invalid_mask[:, 0, :, :] < 0.5
        uncertainty_map[gt_invalid_mask] = -1e4

    # (B, H*W)
    _, idx = uncertainty_map.view(B, -1).sort(1, descending=True)

    # importance sampling
    if int(beta * N) > 0:
        importance = idx[:, :int(beta * N)]    # B, beta*N

        # remaining
        remaining = idx[:, int(beta * N):]     # B, H*W - beta*N

        # coverage
        num_coverage = N - int(beta * N)

        if num_coverage <= 0:
            samples = importance
        else:
            coverage_list = []
            for i in range(B):
                idx_c = torch.randperm(remaining.size()[1])    # shuffles "H*W - beta*N"
                coverage_list.append(remaining[i, :][idx_c[:num_coverage]].view(1, -1))     # 1, N-beta*N
            coverage = torch.cat(coverage_list, dim=0)                                      # B, N-beta*N
            samples = torch.cat((importance, coverage), dim=1)                              # B, N

    else:
        # remaining
        remaining = idx[:, :]  # B, H*W

        # coverage
        num_coverage = N

        coverage_list = []
        for i in range(B):
            idx_c = torch.randperm(remaining.size()[1])  # shuffles "H*W - beta*N"
            coverage_list.append(remaining[i, :][idx_c[:num_coverage]].view(1, -1))  # 1, N-beta*N
        coverage = torch.cat(coverage_list, dim=0)  # B, N-beta*N
        samples = coverage

    # point coordinates
    rows_int = samples // W         # 0 for first row, H-1 for last row
    rows_float = rows_int / float(H-1)         # 0 to 1.0
    rows_float = (rows_float * 2.0) - 1.0       # -1.0 to 1.0

    cols_int = samples % W          # 0 for first column, W-1 for last column
    cols_float = cols_int / float(W-1)         # 0 to 1.0
    cols_float = (cols_float * 2.0) - 1.0       # -1.0 to 1.0

    point_coords = torch.zeros(B, 1, N, 2)
    point_coords[:, 0, :, 0] = cols_float             # x coord
    point_coords[:, 0, :, 1] = rows_float             # y coord
    point_coords = point_coords.to(device)
    return point_coords, rows_int, cols_int
    
class FlowHead(nn.Module):
    def __init__(self, input_dim=128, hidden_dim=256, output_dim_depth=2, output_dim_norm=4,output_dim_rm=3):
        super(FlowHead, self).__init__()
        self.conv1d = nn.Conv2d(input_dim, hidden_dim // 2, 3, padding=1)
        self.conv2d = nn.Conv2d(hidden_dim // 2, output_dim_depth, 3, padding=1)

        self.conv1n = nn.Conv2d(input_dim, hidden_dim // 2, 3, padding=1)
        self.conv2n = nn.Conv2d(hidden_dim // 2, output_dim_norm, 3, padding=1)
        self.relu = nn.ReLU(inplace=True)

        self.conv1rm= nn.Conv2d(input_dim, hidden_dim // 2, 3, padding=1)
        self.conv2rm= nn.Conv2d(hidden_dim // 2, output_dim_rm, 3, padding=1)

    def forward(self, x):
        depth = self.conv2d(self.relu(self.conv1d(x)))
        normal = self.conv2n(self.relu(self.conv1n(x)))
        rm= self.conv2rm(self.relu(self.conv1rm(x)))

        return torch.cat((depth, normal,rm), dim=1)
        

class ConvGRU(nn.Module):
    def __init__(self, hidden_dim, input_dim, kernel_size=3):
        super(ConvGRU, self).__init__()
        self.convz = nn.Conv2d(hidden_dim+input_dim, hidden_dim, kernel_size, padding=kernel_size//2)
        self.convr = nn.Conv2d(hidden_dim+input_dim, hidden_dim, kernel_size, padding=kernel_size//2)
        self.convq = nn.Conv2d(hidden_dim+input_dim, hidden_dim, kernel_size, padding=kernel_size//2)

    def forward(self, h, cz, cr, cq, *x_list):
        x = torch.cat(x_list, dim=1)
        hx = torch.cat([h, x], dim=1)

        z = torch.sigmoid((self.convz(hx) + cz))
        r = torch.sigmoid((self.convr(hx) + cr))
        q = torch.tanh((self.convq(torch.cat([r*h, x], dim=1)) + cq))

        # z = torch.sigmoid((self.convz(hx) + cz).float())
        # r = torch.sigmoid((self.convr(hx) + cr).float())
        # q = torch.tanh((self.convq(torch.cat([r*h, x], dim=1)) + cq).float())

        h = (1-z) * h + z * q
        return h

def pool2x(x):
    return F.avg_pool2d(x, 3, stride=2, padding=1)

def pool4x(x):
    return F.avg_pool2d(x, 5, stride=4, padding=1)

def interp(x, dest):
    interp_args = {'mode': 'bilinear', 'align_corners': True}
    return interpolate_float32(x, dest.shape[2:], **interp_args)

class BasicMultiUpdateBlock(nn.Module):
    def __init__(self, args, hidden_dims=[], out_dims=2):
        super().__init__()
        self.args = args
        self.n_gru_layers = args.model.decode_head.n_gru_layers # 3
        self.n_downsample = args.model.decode_head.n_downsample # 3, resolution of the disparity field (1/2^K)
        
        # self.encoder = BasicMotionEncoder(args)
        # encoder_output_dim = 128 # if there is corr volume
        encoder_output_dim = 6 # no corr volume

        self.gru08 = ConvGRU(hidden_dims[2], encoder_output_dim+3 + hidden_dims[1] * (self.n_gru_layers > 1))
        self.gru16 = ConvGRU(hidden_dims[1], hidden_dims[0] * (self.n_gru_layers == 3) + hidden_dims[2])
        self.gru32 = ConvGRU(hidden_dims[0], hidden_dims[1])
        self.flow_head = FlowHead(hidden_dims[2], hidden_dim=2*hidden_dims[2])
        factor = 2**self.n_downsample

        self.mask = nn.Sequential(
            nn.Conv2d(hidden_dims[2], hidden_dims[2], 3, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(hidden_dims[2], (factor**2)*9, 1, padding=0))

    def forward(self, net, inp, corr=None, flow=None, iter08=True, iter16=True, iter32=True, update=True):

        if iter32:
            net[2] = self.gru32(net[2], *(inp[2]), pool2x(net[1]))
        if iter16:
            if self.n_gru_layers > 2:
                net[1] = self.gru16(net[1], *(inp[1]), interp(pool2x(net[0]), net[1]), interp(net[2], net[1]))
            else:
                net[1] = self.gru16(net[1], *(inp[1]), interp(pool2x(net[0]), net[1]))
        if iter08:
            if corr is not None:
                motion_features = self.encoder(flow, corr)
            else:
                motion_features = flow
            if self.n_gru_layers > 1:
                net[0] = self.gru08(net[0], *(inp[0]), motion_features, interp(net[1], net[0]))
            else:
                net[0] = self.gru08(net[0], *(inp[0]), motion_features)

        if not update:
            return net

        delta_flow = self.flow_head(net[0])

        # scale mask to balence gradients
        mask = .25 * self.mask(net[0])
        return net, mask, delta_flow

class LayerNorm2d(nn.LayerNorm):
    def __init__(self, dim):
        super(LayerNorm2d, self).__init__(dim)

    def forward(self, x):
        x = x.permute(0, 2, 3, 1).contiguous()
        x = super(LayerNorm2d, self).forward(x)
        x = x.permute(0, 3, 1, 2).contiguous()
        return x

class ResidualBlock(nn.Module):
    def __init__(self, in_planes, planes, norm_fn='group', stride=1):
        super(ResidualBlock, self).__init__()
  
        self.conv1 = nn.Conv2d(in_planes, planes, kernel_size=3, padding=1, stride=stride)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, padding=1)
        self.relu = nn.ReLU(inplace=True)

        num_groups = planes // 8

        if norm_fn == 'group':
            self.norm1 = nn.GroupNorm(num_groups=num_groups, num_channels=planes)
            self.norm2 = nn.GroupNorm(num_groups=num_groups, num_channels=planes)
            if not (stride == 1 and in_planes == planes):
                self.norm3 = nn.GroupNorm(num_groups=num_groups, num_channels=planes)
        
        elif norm_fn == 'batch':
            self.norm1 = nn.BatchNorm2d(planes)
            self.norm2 = nn.BatchNorm2d(planes)
            if not (stride == 1 and in_planes == planes):
                self.norm3 = nn.BatchNorm2d(planes)
        
        elif norm_fn == 'instance':
            self.norm1 = nn.InstanceNorm2d(planes)
            self.norm2 = nn.InstanceNorm2d(planes)
            if not (stride == 1 and in_planes == planes):
                self.norm3 = nn.InstanceNorm2d(planes)

        elif norm_fn == 'layer':
            self.norm1 = LayerNorm2d(planes)
            self.norm2 = LayerNorm2d(planes)
            if not (stride == 1 and in_planes == planes):
                self.norm3 = LayerNorm2d(planes)

        elif norm_fn == 'none':
            self.norm1 = nn.Sequential()
            self.norm2 = nn.Sequential()
            if not (stride == 1 and in_planes == planes):
                self.norm3 = nn.Sequential()

        if stride == 1 and in_planes == planes:
            self.downsample = None
        
        else:    
            self.downsample = nn.Sequential(
                nn.Conv2d(in_planes, planes, kernel_size=1, stride=stride), self.norm3)
            
    def forward(self, x):
        y = x
        y = self.conv1(y)
        y = self.norm1(y)
        y = self.relu(y)
        y = self.conv2(y)
        y = self.norm2(y)
        y = self.relu(y)

        if self.downsample is not None:
            x = self.downsample(x)

        return self.relu(x+y)


class ContextFeatureEncoder(nn.Module):
    '''
    Encoder features are used to:
        1. initialize the hidden state of the update operator 
        2. and also injected into the GRU during each iteration of the update operator
    '''
    def __init__(self, in_dim, output_dim):
        '''
        in_dim     = [x4, x8, x16, x32]
        output_dim = [hindden_dims,   context_dims]
                    [[x4,x8,x16,x32],[x4,x8,x16,x32]]
        '''
        super().__init__()

        output_list = []
        for dim in output_dim:
            conv_out = nn.Sequential(
                ResidualBlock(in_dim[0], dim[0], 'layer', stride=1),
                nn.Conv2d(dim[0], dim[0], 3, padding=1))
            output_list.append(conv_out)

        self.outputs04 = nn.ModuleList(output_list)

        output_list = []
        for dim in output_dim:
            conv_out = nn.Sequential(
                ResidualBlock(in_dim[1], dim[1], 'layer', stride=1),
                nn.Conv2d(dim[1], dim[1], 3, padding=1))
            output_list.append(conv_out)

        self.outputs08 = nn.ModuleList(output_list)

        output_list = []
        for dim in output_dim:
            conv_out = nn.Sequential(
                ResidualBlock(in_dim[2], dim[2], 'layer', stride=1),
                nn.Conv2d(dim[2], dim[2], 3, padding=1))
            output_list.append(conv_out)

        self.outputs16 = nn.ModuleList(output_list)

        # output_list = []
        # for dim in output_dim:
        #     conv_out = nn.Conv2d(in_dim[3], dim[3], 3, padding=1)
        #     output_list.append(conv_out)

        # self.outputs32 = nn.ModuleList(output_list)

    def forward(self, encoder_features):
        x_4, x_8, x_16, x_32 = encoder_features

        outputs04 = [f(x_4) for f in self.outputs04]
        outputs08 = [f(x_8) for f in self.outputs08]
        outputs16 = [f(x_16)for f in self.outputs16]
        # outputs32 = [f(x_32) for f in self.outputs32]

        return (outputs04, outputs08, outputs16)

class ConvBlock(nn.Module):
    # reimplementation of DPT
    def __init__(self, channels):
        super(ConvBlock, self).__init__()

        self.act = nn.ReLU(inplace=True)
        self.conv1 = nn.Conv2d(
            channels,
            channels,
            kernel_size=3,
            stride=1,
            padding=1
        )
        self.conv2 = nn.Conv2d(
            channels,
            channels,
            kernel_size=3,
            stride=1,
            padding=1
        )

    def forward(self, x):
        out = self.act(x)
        out = self.conv1(out)
        out = self.act(out)
        out = self.conv2(out)
        return x + out

class FuseBlock(nn.Module):
    # reimplementation of DPT
    def __init__(self, in_channels, out_channels, fuse=True, upsample=True, scale_factor=2):
        super(FuseBlock, self).__init__()

        self.fuse = fuse
        self.scale_factor = scale_factor
        self.way_trunk = ConvBlock(in_channels)
        if self.fuse:
            self.way_branch = ConvBlock(in_channels)
        
        self.out_conv = nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=1,
            stride=1,
            padding=0,
        )
        self.upsample = upsample

    def forward(self, x1, x2=None):
        if x2 is not None:
            x2 = self.way_branch(x2)
            x1 = x1 + x2

        out = self.way_trunk(x1)

        if self.upsample:
            out = interpolate_float32(
                out, scale_factor=self.scale_factor, mode="bilinear", align_corners=True
            )
        out = self.out_conv(out)
        return out

class Readout(nn.Module):  
    # From DPT
    def __init__(self, in_features, use_cls_token=True, num_register_tokens=0):
        super(Readout, self).__init__()
        self.use_cls_token = use_cls_token
        if self.use_cls_token == True:
            self.project_patch = nn.Linear(in_features, in_features)
            self.project_learn = nn.Linear((1 + num_register_tokens) * in_features, in_features, bias=False) 
            self.act = nn.GELU()
        else:
            self.project = nn.Identity()

    def forward(self, x):

        if self.use_cls_token == True:
            x_patch = self.project_patch(x[0])
            x_learn = self.project_learn(x[1])
            x_learn = x_learn.expand_as(x_patch).contiguous()
            features = x_patch + x_learn
            return self.act(features)
        else:
            return self.project(x)

class Token2Feature(nn.Module):
    # From DPT
    def __init__(self, vit_channel, feature_channel, scale_factor, use_cls_token=True, num_register_tokens=0):
        super(Token2Feature, self).__init__()
        self.scale_factor = scale_factor
        self.readoper = Readout(in_features=vit_channel, use_cls_token=use_cls_token, num_register_tokens=num_register_tokens)
        if scale_factor > 1 and isinstance(scale_factor, int):
            self.sample = nn.ConvTranspose2d(
                in_channels=vit_channel,
                out_channels=feature_channel,
                kernel_size=scale_factor,
                stride=scale_factor,
                padding=0,
            )
        
        elif scale_factor > 1:
            self.sample = nn.Sequential(
                # Upsample2(upscale=scale_factor),
                # nn.Upsample(scale_factor=scale_factor),
                nn.Conv2d(
                    in_channels=vit_channel,
                    out_channels=feature_channel,
                    kernel_size=1,
                    stride=1,
                    padding=0,
                ),
            )
            

        elif scale_factor < 1:
            scale_factor = int(1.0 / scale_factor)
            self.sample = nn.Conv2d(
                in_channels=vit_channel,
                out_channels=feature_channel,
                kernel_size=scale_factor+1,
                stride=scale_factor,
                padding=1,
            )

        else:
            self.sample = nn.Identity()

    def forward(self, x):
        x = self.readoper(x)
        #if use_cls_token == True:
        x = x.permute(0, 3, 1, 2).contiguous()
        if isinstance(self.scale_factor, float):
            x = interpolate_float32(x.float(), scale_factor=self.scale_factor, mode='nearest')
        x = self.sample(x)
        return x

class EncoderFeature(nn.Module):
    def __init__(self, vit_channel, num_ch_dec=[256, 512, 1024, 1024], use_cls_token=True, num_register_tokens=0):
        super(EncoderFeature, self).__init__()
        self.vit_channel = vit_channel
        self.num_ch_dec = num_ch_dec

        self.read_3 = Token2Feature(self.vit_channel, self.num_ch_dec[3], scale_factor=1, use_cls_token=use_cls_token, num_register_tokens=num_register_tokens)
        self.read_2 = Token2Feature(self.vit_channel, self.num_ch_dec[2], scale_factor=1, use_cls_token=use_cls_token, num_register_tokens=num_register_tokens)
        self.read_1 = Token2Feature(self.vit_channel, self.num_ch_dec[1], scale_factor=2, use_cls_token=use_cls_token, num_register_tokens=num_register_tokens)
        self.read_0 = Token2Feature(self.vit_channel, self.num_ch_dec[0], scale_factor=7/2, use_cls_token=use_cls_token, num_register_tokens=num_register_tokens)

    def forward(self, ref_feature):
        x = self.read_3(ref_feature[3])  # 1/14
        x2 = self.read_2(ref_feature[2]) # 1/14
        x1 = self.read_1(ref_feature[1]) # 1/7
        x0 = self.read_0(ref_feature[0]) # 1/4

        return x, x2, x1, x0

class DecoderFeature(nn.Module):
    def __init__(self, vit_channel, num_ch_dec=[128, 256, 512, 1024, 1024], use_cls_token=True):
        super(DecoderFeature, self).__init__()
        self.vit_channel = vit_channel
        self.num_ch_dec = num_ch_dec

        self.upconv_3 = FuseBlock(
            self.num_ch_dec[4], 
            self.num_ch_dec[3], 
        fuse=False, upsample=False)
        
        self.upconv_2 = FuseBlock(
            self.num_ch_dec[3], 
            self.num_ch_dec[2],
        )
        
        self.upconv_1 = FuseBlock(
            self.num_ch_dec[2], 
            self.num_ch_dec[1] + 2,
            scale_factor=7/4
        )

        # self.upconv_0 = FuseBlock(
        #     self.num_ch_dec[1], 
        #     self.num_ch_dec[0] + 1,
        # )
    
    def forward(self, ref_feature):
        x, x2, x1, x0 = ref_feature # 1/14 1/14 1/7 1/4
     
        x = self.upconv_3(x)     # 1/14
        x = self.upconv_2(x, x2) # 1/7
        x = self.upconv_1(x, x1) # 1/4
        # x = self.upconv_0(x, x0) # 4/7
        return x
# class Gray_to_feature(nn.Module):
#     """
#     this is a experimental head for converting features to grayscale image to train it to generate roughness map and can be scaled to generate metallic maps 
#     """
#     def __init__(self, input_channels=1, output_channels=64):
#         super(Gray_to_feature, self).__init__()
#         self.upscale = nn.Sequential(
#             # Reduce channels while keeping spatial dimensions the same
#             nn.Conv2d(input_channels, 32, kernel_size=3, stride=1, padding=1),
#             nn.ReLU(),
#             nn.Conv2d(32, 64, kernel_size=3, stride=1, padding=1),
#             # Increase spatial dimensions by a factor of 2 (scale_factor = 4 implies 2x2 upsampling twice)
#             nn.ReLU(),
#             nn.Conv2d(64, 128, kernel_size=3, stride=1, padding=1),
#             nn.ReLU(),
#             nn.Conv2d(128, 64, kernel_size=3, stride=1, padding=1),
#         )



    #     self.upscale = nn.Sequential(
    #         # Reduce channels while keeping spatial dimensions the same
    #         nn.Conv2d(input_channels, 64, kernel_size=3, stride=1, padding=1),
    #         nn.ReLU(),

    #         # Increase spatial dimensions by a factor of 2 (scale_factor = 4 implies 2x2 upsampling twice)
    #         nn.ConvTranspose2d(64, 32, kernel_size=4, stride=2, padding=1),
    #         nn.ReLU(),
    #         nn.ConvTranspose2d(32, output_channels, kernel_size=4, stride=2, padding=1)
    #     )

    # def forward(self, x):
    #     return self.upscale(x)


# class Upscale_roughness(nn.Module):
#     """
#     this is a experimental head for converting features to grayscale image to train it to generate roughness map and can be scaled to generate metallic maps 
#     """
#     def __init__(self, input_channels=1, output_channels=32):
#         super(Upscale_roughness, self).__init__()
#         self.upscale = nn.Sequential(
#             # Reduce channels while keeping spatial dimensions the same
#             nn.Conv2d(input_channels, 128, kernel_size=3, stride=1, padding=1),
#             nn.ReLU(),

#             # Increase spatial dimensions by a factor of 2 (scale_factor = 4 implies 2x2 upsampling twice)
#             nn.ConvTranspose2d(128, 64, kernel_size=4, stride=2, padding=1),
#             nn.ReLU(),
#             nn.ConvTranspose2d(64, output_channels, kernel_size=4, stride=2, padding=1)
#         )

#     def forward(self, x):
#         return self.upscale(x)

class Roughness_head(nn.Module):
   
    def __init__(self, input_channels=96):
        super(Roughness_head, self).__init__()
        self.channel_fix = nn.Conv2d(input_channels,64, kernel_size=3, stride=1, padding=1)
        self.conv_blk1=ConvBlock(channels=64)
        self.conv_blk2=ConvBlock(channels=64)
        self.head=nn.Sequential(
            # Reduce channels while keeping spatial dimensions the same
            nn.Conv2d(64,32, kernel_size=3, stride=1, padding=1),
            nn.Conv2d(32, 1, kernel_size=3, stride=1, padding=1),

            )
            

    def forward(self, x):
        return self.head(self.conv_blk2(self.conv_blk1(self.channel_fix(x))))


class RAFTDepthNormalDPT5(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.in_channels = cfg.model.decode_head.in_channels # [1024, 1024, 1024, 1024]
        self.feature_channels = cfg.model.decode_head.feature_channels # [256, 512, 1024, 1024] [2/7, 1/7, 1/14, 1/14]
        self.decoder_channels = cfg.model.decode_head.decoder_channels # [128, 256, 512, 1024, 1024] [-, 1/4, 1/7, 1/14, 1/14]
        self.use_cls_token = cfg.model.decode_head.use_cls_token
        self.up_scale = cfg.model.decode_head.up_scale
        self.num_register_tokens = cfg.model.decode_head.num_register_tokens
        self.min_val = cfg.data_basic.depth_normalize[0]
        self.max_val = cfg.data_basic.depth_normalize[1]
        self.regress_scale = 100.0

        self.hidden_dims = self.context_dims = cfg.model.decode_head.hidden_channels # [128, 128, 128, 128]
        self.n_gru_layers = cfg.model.decode_head.n_gru_layers # 3
        self.n_downsample = cfg.model.decode_head.n_downsample # 3, resolution of the disparity field (1/2^K)
        self.iters = cfg.model.decode_head.iters # 22 actually 4 prediction run 3 
        self.slow_fast_gru = cfg.model.decode_head.slow_fast_gru # True

        self.num_depth_regressor_anchor = 256
        self.num_roughness_regressor_anchor = 256 # 512
        self.used_res_channel = self.decoder_channels[1] # now, use 2/7 res
        self.token2feature = EncoderFeature(self.in_channels[0], self.feature_channels, self.use_cls_token, self.num_register_tokens)
        self.decoder_mono = DecoderFeature(self.in_channels, self.decoder_channels)
        self.depth_regressor = nn.Sequential(
            nn.Conv2d(self.used_res_channel,
                      self.num_depth_regressor_anchor,
                      kernel_size=3,
                      padding=1),
            # nn.BatchNorm2d(self.num_depth_regressor_anchor),
            nn.ReLU(inplace=True),
            nn.Conv2d(self.num_depth_regressor_anchor,
                      self.num_depth_regressor_anchor,
                      kernel_size=1),
        )
        # self.roughness_regressor = nn.Sequential(
        #         nn.Upsample(scale_factor=4, mode='bilinear', align_corners=True),  # First, upscale spatial dimensions
        #         nn.Conv2d(
        #             self.used_res_channel,
        #             self.num_roughness_regressor_anchor,
        #             kernel_size=3,
        #             padding=1
        #         ),
        #         nn.ReLU(inplace=True),
        #         nn.Conv2d(
        #             self.num_roughness_regressor_anchor,
        #             self.num_roughness_regressor_anchor,
        #             kernel_size=1
        #         )
        #     )
        self.normal_predictor = nn.Sequential(
            nn.Conv2d(self.used_res_channel,
                      128,
                      kernel_size=3,
                      padding=1),
            # nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 128, kernel_size=1), nn.ReLU(inplace=True),
            nn.Conv2d(128, 128, kernel_size=1), nn.ReLU(inplace=True),
            nn.Conv2d(128, 3, kernel_size=1),
        )
        self.roughness_predictor=nn.Sequential(
            nn.Conv2d(self.used_res_channel,
                      128,
                      kernel_size=3,
                      padding=1),
            # nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 128, kernel_size=1), nn.ReLU(inplace=True),
            nn.Conv2d(128, 128, kernel_size=1), nn.ReLU(inplace=True),
            nn.Conv2d(128, 3, kernel_size=1),
        )
        #self.roughness_head=nn.Sequential(nn.Conv2d(self.used_res_channel+1,
        #               128,
        #               kernel_size=3,
        #               padding=1),
        #     # nn.BatchNorm2d(128),
        #     nn.ReLU(inplace=True),
        #     nn.Conv2d(128, 128, kernel_size=3,padding=1), nn.ReLU(inplace=True),
        #     nn.Conv2d(128, 64, kernel_size=3,padding=1), nn.ReLU(inplace=True),
        #     nn.Conv2d(64, 32, kernel_size=3,padding=1), nn.ReLU(inplace=True),
        #     nn.Conv2d(32, 32, kernel_size=3,padding=1), nn.ReLU(inplace=True),
        #     nn.Conv2d(32, 32, kernel_size=3,padding=1), nn.ReLU(inplace=True),
        #     nn.Conv2d(32, 32, kernel_size=3,padding=1), nn.ReLU(inplace=True),
        #     nn.Conv2d(32, 32, kernel_size=3,padding=1), nn.ReLU(inplace=True),
        #     nn.Conv2d(32, 1, kernel_size=3,padding=1)
            
        #     )

        # self.ao_head=nn.Sequential(nn.Conv2d(1,
        #               128,
        #               kernel_size=3,
        #               padding=1),
        #     # nn.BatchNorm2d(128),
        #     nn.ReLU(inplace=True),
        #     nn.Conv2d(128, 128, kernel_size=3,padding=1), nn.ReLU(inplace=True),
        #     nn.Conv2d(128, 64, kernel_size=3,padding=1), nn.ReLU(inplace=True),
        #     nn.Conv2d(64, 32, kernel_size=3,padding=1), nn.ReLU(inplace=True),
        #     nn.Conv2d(32, 32, kernel_size=3,padding=1), nn.ReLU(inplace=True),
        #     nn.Conv2d(32, 32, kernel_size=3,padding=1), nn.ReLU(inplace=True),
        #     nn.Conv2d(32, 32, kernel_size=3,padding=1), nn.ReLU(inplace=True),
        #     nn.Conv2d(32, 32, kernel_size=3,padding=1), nn.ReLU(inplace=True),
        #     nn.Conv2d(32, 1, kernel_size=3,padding=1)
            
        #     )


        # self.roughness_head=Roughness_head(input_channels=96)
        self.context_feature_encoder = ContextFeatureEncoder(self.feature_channels, [self.hidden_dims, self.context_dims])
        self.context_zqr_convs = nn.ModuleList([nn.Conv2d(self.context_dims[i], self.hidden_dims[i]*3, 3, padding=3//2) for i in range(self.n_gru_layers)])
        self.update_block = BasicMultiUpdateBlock(cfg, hidden_dims=self.hidden_dims, out_dims=6)
        # self.gray_feature=Gray_to_feature(input_channels=1, output_channels=64)
        # self.up_roughness=Upscale_roughness(input_channels=self.used_res_channel,output_channels=32)
        self.relu = nn.ReLU(inplace=True)
        self.pratham=None
        # self.roughness_head=FeatureToGrayScale(input_channels=self.num_roughness_regressor_anchor, output_channels=1, scale_factor=4)
    
    def get_bins(self, bins_num):
        depth_bins_vec = torch.linspace(math.log(self.min_val), math.log(self.max_val), bins_num, device="cuda")
        depth_bins_vec = torch.exp(depth_bins_vec)
        return depth_bins_vec
    
    def register_depth_expectation_anchor(self, bins_num, B):
        depth_bins_vec = self.get_bins(bins_num)
        depth_bins_vec = depth_bins_vec.unsqueeze(0).repeat(B, 1)        
        self.register_buffer('depth_expectation_anchor', depth_bins_vec, persistent=False)

    # def register_roughness_expectation_anchor(self, bins_num, B):
    #     roughness_bins_vec = self.get_bins(bins_num)
    #     roughness_bins_vec = roughness_bins_vec.unsqueeze(0).repeat(B, 1)        
    #     self.register_buffer('roughness_expectation_anchor', roughness_bins_vec, persistent=False)
    
    def clamp(self, x):
        y = self.relu(x - self.min_val) + self.min_val
        y = self.max_val - self.relu(self.max_val - y)
        return y
    
    def regress_depth(self, feature_map_d):
        prob_feature = self.depth_regressor(feature_map_d)
        prob = prob_feature.softmax(dim=1)
        # prob = prob_feature.float().softmax(dim=1)

        ## Error logging
        if torch.isnan(prob).any():
            print('prob_feat_nan!!!')
        if torch.isinf(prob).any():
            print('prob_feat_inf!!!')

        # h = prob[0,:,0,0].cpu().numpy().reshape(-1)
        # import matplotlib.pyplot as plt 
        # plt.bar(range(len(h)), h)
        B = prob.shape[0]
        if "depth_expectation_anchor" not in self._buffers:
            self.register_depth_expectation_anchor(self.num_roughness_regressor_anchor, B)
        d = compute_depth_expectation(
            prob,
            self.depth_expectation_anchor[:B, ...]).unsqueeze(1)

        ## Error logging
        if torch.isnan(d ).any():
            print('d_nan!!!')
        if torch.isinf(d ).any():
            print('d_inf!!!')

        return (self.clamp(d) - self.max_val)/ self.regress_scale, prob_feature


    # def regress_roughness(self, feature_map_d):
    #     prob_feature = self.roughness_regressor(feature_map_d)
    #     prob = prob_feature.softmax(dim=1)
    #     #prob = prob_feature.float().softmax(dim=1)

    #     ## Error logging
    #     if torch.isnan(prob).any():
    #         print('prob_feat_nan!!!')
    #     if torch.isinf(prob).any():
    #         print('prob_feat_inf!!!')

    #     # h = prob[0,:,0,0].cpu().numpy().reshape(-1)
    #     # import matplotlib.pyplot as plt 
    #     # plt.bar(range(len(h)), h)
    #     B = prob.shape[0]
    #     if "roughness_expectation_anchor" not in self._buffers:
    #         self.register_roughness_expectation_anchor(self.num_depth_regressor_anchor, B)
    #     d = compute_roughness_expectation(
    #         prob,
    #         self.roughness_expectation_anchor[:B, ...]).unsqueeze(1)

    #     ## Error logging
    #     if torch.isnan(d ).any():
    #         print('d_nan!!!')
    #     if torch.isinf(d ).any():
    #         print('d_inf!!!')

    #     return (self.clamp(d) - self.max_val)/ self.regress_scale, prob_feature

    def pred_normal(self, feature_map, confidence):
        normal_out = self.normal_predictor(feature_map)

        ## Error logging
        if torch.isnan(normal_out).any():
            print('norm_nan!!!')
        if torch.isinf(normal_out).any():
            print('norm_feat_inf!!!')

        return norm_normalize(torch.cat([normal_out, confidence], dim=1))
        # return norm_normalize(torch.cat([normal_out, confidence], dim=1).float())

    def pred_roughness(self, feature_map, confidence):
        roughness_out = self.roughness_predictor(feature_map)

        ## Error logging
        if torch.isnan(roughness_out).any():
            print('roughness_nan!!!')
        if torch.isinf(roughness_out).any():
            print('roughness_inf!!!')
        return norm_normalize_single_channel(roughness_out)
        
    
    def create_mesh_grid(self, height, width, batch, device="cuda", set_buffer=True):
        y, x = torch.meshgrid([torch.arange(0, height, dtype=torch.float32, device=device),
                               torch.arange(0, width, dtype=torch.float32, device=device)], indexing='ij')
        meshgrid = torch.stack((x, y))
        meshgrid = meshgrid.unsqueeze(0).repeat(batch, 1, 1, 1)
        #self.register_buffer('meshgrid', meshgrid, persistent=False)
        return meshgrid

    def upsample_flow(self, flow, mask):
        """ Upsample flow field [H/8, W/8, 2] -> [H, W, 2] using convex combination """
        N, D, H, W = flow.shape
        factor = 2 ** self.n_downsample
        mask = mask.view(N, 1, 9, factor, factor, H, W)
        mask = torch.softmax(mask, dim=2)
        #mask = torch.softmax(mask.float(), dim=2)

        #up_flow = F.unfold(factor * flow, [3,3], padding=1)
        up_flow = F.unfold(flow, [3,3], padding=1)
        up_flow = up_flow.view(N, D, 9, 1, 1, H, W)

        up_flow = torch.sum(mask * up_flow, dim=2)
        up_flow = up_flow.permute(0, 1, 4, 2, 5, 3)
        return up_flow.reshape(N, D, factor*H, factor*W)

    def initialize_flow(self, img):
        """ Flow is represented as difference between two coordinate grids flow = coords1 - coords0"""
        N, _, H, W = img.shape

        coords0 = coords_grid(N, H, W).to(img.device)
        coords1 = coords_grid(N, H, W).to(img.device)

        return coords0, coords1
    
    def upsample(self, x, scale_factor=2):
        """Upsample input tensor by a factor of 2
        """
        return interpolate_float32(x, scale_factor=scale_factor*self.up_scale/8, mode="nearest")

    def forward(self, vit_features,gray_images, **kwargs):
        ## read vit token to multi-scale features
        B, H, W, _, _, num_register_tokens = vit_features[1]
        vit_features = vit_features[0]

        ## Error logging
        if torch.isnan(vit_features[0]).any():
            print('vit_feature_nan!!!')
        if torch.isinf(vit_features[0]).any():
            print('vit_feature_inf!!!')

        if self.use_cls_token == True:
            vit_features = [[ft[:, 1+num_register_tokens:, :].view(B, H, W, self.in_channels[0]), \
                ft[:, 0:1+num_register_tokens, :].view(B, 1, 1, self.in_channels[0] * (1+num_register_tokens))] for ft in vit_features]
        else:
            vit_features = [ft.view(B, H, W, self.in_channels[0]) for ft in vit_features]
        encoder_features = self.token2feature(vit_features) # 1/14, 1/14, 1/7, 1/4

        ## Error logging
        for en_ft in encoder_features:
            if torch.isnan(en_ft).any():
                print('decoder_feature_nan!!!')
                print(en_ft.shape)
            if torch.isinf(en_ft).any():
                print('decoder_feature_inf!!!')
                print(en_ft.shape)

        ## decode features to init-depth (and confidence)
        ref_feat= self.decoder_mono(encoder_features) # now, 1/4 for depth
        
        ## Error logging
        if torch.isnan(ref_feat).any():
            print('ref_feat_nan!!!')
        if torch.isinf(ref_feat).any():
            print('ref_feat_inf!!!')

        feature_map = ref_feat[:, :-2, :, :] # feature map share of depth and normal prediction
        depth_confidence_map = ref_feat[:, -2:-1, :, :]
        normal_confidence_map = ref_feat[:, -1:, :, :]
        depth_pred, binmap = self.regress_depth(feature_map) # regress bin for depth
        normal_pred = self.pred_normal(feature_map, normal_confidence_map) # mlp for normal

        roughness_pred = self.pred_roughness(feature_map, depth_confidence_map)
        # gray_images = gray_images.unsqueeze(1)
        # gray_feat=self.gray_feature(gray_images)
        # up_feat=self.up_roughness(feature_map)
        # feature_map=interpolate_float32(feature_map, scale_factor=4, mode='bilinear', align_corners=True)
    
        # feature_map=torch.cat((gray_feat,up_feat),dim=1)
        # self.pratham={"gray_images":gray_images,"depth_pred":depth_pred,"normal_pred":normal_pred,"roughness_pred":roughness_pred}
        # roughness_pred=torch.cat((gray_images,roughness_pred),dim=1)
        # roughness_pred=self.roughness_head(feature_map)
        depth_init = torch.cat((depth_pred, depth_confidence_map, normal_pred,roughness_pred), dim=1) # (N, 1+1+4, H, W)
        # self.pratham = roughness_pred
        ## encoder features to context-feature for init-hidden-state and contex-features
        cnet_list = self.context_feature_encoder(encoder_features[::-1])
        net_list = [torch.tanh(x[0]) for x in cnet_list] # x_4, x_8, x_16 of hidden state
        inp_list = [torch.relu(x[1]) for x in cnet_list] # x_4, x_8, x_16 context features

        # Rather than running the GRU's conv layers on the context features multiple times, we do it once at the beginning 
        inp_list = [list(conv(i).split(split_size=conv.out_channels//3, dim=1)) for i,conv in zip(inp_list, self.context_zqr_convs)]

        coords0, coords1 = self.initialize_flow(net_list[0])
        if depth_init is not None:
            coords1 = coords1 + depth_init

        if self.training:
            low_resolution_init = [self.clamp(depth_init[:,:1] * self.regress_scale + self.max_val), depth_init[:,1:2], norm_normalize(depth_init[:,2:6].clone(),)]
            init_depth = upflow4(depth_init)
            flow_predictions = [self.clamp(init_depth[:,:1] * self.regress_scale + self.max_val)]
            conf_predictions = [init_depth[:,1:2]]
            normal_outs = [norm_normalize(init_depth[:,2:6].clone())]

        else:
            flow_predictions = []
            conf_predictions = []
            samples_pred_list = []
            coord_list = []
            normal_outs = []
            low_resolution_init = []
            rm_outs=[]

        for itr in range(self.iters):
            # coords1 = coords1.detach()
            flow = coords1 - coords0
            if self.n_gru_layers == 3 and self.slow_fast_gru: # Update low-res GRU
                net_list = self.update_block(net_list, inp_list, iter32=True, iter16=False, iter08=False, update=False)
            if self.n_gru_layers >= 2 and self.slow_fast_gru:# Update low-res GRU and mid-res GRU
                net_list = self.update_block(net_list, inp_list, iter32=self.n_gru_layers==3, iter16=True, iter08=False, update=False)
            net_list, up_mask, delta_flow = self.update_block(net_list, inp_list, None, flow, iter32=self.n_gru_layers==3, iter16=self.n_gru_layers>=2)
            
            # F(t+1) = F(t) + \Delta(t)
            coords1 = coords1 + delta_flow

            # We do not need to upsample or output intermediate results in test_mode
            #if (not self.training) and itr < self.iters-1:
                #continue

            # upsample predictions
            if up_mask is None:
                flow_up = self.upsample(coords1-coords0, 4)
            else:
                flow_up = self.upsample_flow(coords1 - coords0, up_mask)
                # flow_up = self.upsample(coords1-coords0, 4)

            flow_predictions.append(self.clamp(flow_up[:,:1] * self.regress_scale + self.max_val))
            # conf_predictions.append(flow_up[:,1:2])
            normal_outs.append(norm_normalize(flow_up[:,2:6].clone()))
            rm_outs=norm_normalize(flow_up[:,5:])
        
        outputs=dict(
            prediction=flow_predictions[-1],
            predictions_list=flow_predictions,
            # confidence=conf_predictions[-1],
            # confidence_list=conf_predictions,
            pred_logit=None,
            # samples_pred_list=samples_pred_list,
            # coord_list=coord_list,
            # prediction_normal=normal_outs[-1],
            normal_out_list=normal_outs,
            # low_resolution_init=low_resolution_init,
            rm=rm_outs,
        
        )

        return outputs


if __name__ == "__main__":
    try:
        from mmcv.utils import Config
    except:
        from mmengine import Config
    cfg = Config.fromfile('/mu.hu/monodepth/mono/configs/RAFTDecoder/vit.raft.full2t.py')
    cfg.model.decode_head.in_channels = [384, 384, 384, 384]
    cfg.model.decode_head.feature_channels = [96, 192, 384, 768]
    cfg.model.decode_head.decoder_channels = [48, 96, 192, 384, 384]
    cfg.model.decode_head.hidden_channels = [48, 48, 48, 48, 48]
    cfg.model.decode_head.up_scale = 7
    
    # cfg.model.decode_head.use_cls_token = True
    # vit_feature = [[torch.rand((2, 20, 60, 384)).cuda(), torch.rand(2, 384).cuda()], \
    #         [torch.rand((2, 20, 60, 384)).cuda(), torch.rand(2, 384).cuda()], \
    #         [torch.rand((2, 20, 60, 384)).cuda(), torch.rand(2, 384).cuda()], \
    #         [torch.rand((2, 20, 60, 384)).cuda(), torch.rand(2, 384).cuda()]]
    
    cfg.model.decode_head.use_cls_token = True
    cfg.model.decode_head.num_register_tokens = 4
    vit_feature = [[torch.rand((2, (74 * 74) + 5, 384)).cuda(),\
                    torch.rand((2, (74 * 74) + 5, 384)).cuda(), \
                    torch.rand((2, (74 * 74) + 5, 384)).cuda(), \
                    torch.rand((2, (74 * 74) + 5, 384)).cuda()], (2, 74, 74, 1036, 1036, 4)]

    decoder = RAFTDepthNormalDPT5(cfg).cuda()
    output = decoder(vit_feature)
    temp = 1




