import torch
from torch import nn
from torch.nn import functional as F
import numpy as np

from torch.nn.init import calculate_gain, _calculate_correct_fan
from torch.nn.modules.utils import _triple


def num_filters(phase, num_phases, base_dim):
    num_downscales = int(np.log2(base_dim / 16))
    filters = min(base_dim // (2 ** (phase - num_phases + num_downscales)), base_dim)
    return filters


def kaiming_normal_(tensor, gain_mode, mode='fan_in'):
    fan = _calculate_correct_fan(tensor, mode)
    gain = calculate_gain(gain_mode)
    std = gain / np.sqrt(fan)
    with torch.no_grad():
        tensor.normal_(0, 1)
        # print(f'Scaling for {gain_mode}: {std:.3f}')
        return std
    
    
class EqualizedConv3d(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                 padding=0):
        super(EqualizedConv3d, self).__init__()
        kernel_size = _triple(kernel_size)
        stride = _triple(stride)
        padding = _triple(padding)
        
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.stride = stride
        self.padding = padding

        self.weight = nn.Parameter(torch.Tensor(
                out_channels, in_channels, *kernel_size))
        self.bias = nn.Parameter(torch.Tensor(out_channels))
        
        self.std = None  # Placeholder
        self.reset_parameters()

    def reset_parameters(self):
        self.std = kaiming_normal_(self.weight, 'conv3d')
        if self.bias is not None:
            fan_in, _ = torch.nn.init._calculate_fan_in_and_fan_out(self.weight)
            bound = 1 / np.sqrt(fan_in)
            torch.nn.init.uniform_(self.bias, -bound, bound)

    def forward(self, input):
        return F.conv3d(input, self.weight * self.std, self.bias, self.stride,
                        self.padding)

    
class EqualizedLinear(nn.Module):
    def __init__(self, in_features, out_features):
        super(EqualizedLinear, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.weight = nn.Parameter(torch.randn(out_features, in_features))
        self.bias = nn.Parameter(torch.zeros(out_features))
        self.std = None  # Placeholder
        self.reset_parameters()

    def reset_parameters(self):
        self.std = kaiming_normal_(self.weight, 'linear')
        if self.bias is not None:
            fan_in, _ = torch.nn.init._calculate_fan_in_and_fan_out(self.weight)
            bound = 1 / np.sqrt(fan_in)
            torch.nn.init.uniform_(self.bias, -bound, bound)

    def forward(self, input):
        return F.linear(input, self.weight * self.std, self.bias)

    
class DiscriminatorBlock(nn.Sequential):
    def __init__(self, filters_in, filters_out):
        super(DiscriminatorBlock, self).__init__()
        
        self.filters_in = filters_in
        self.filters_out = filters_out
        
        self.conv1 = EqualizedConv3d(filters_in, filters_in, 3, padding=1)
        self.conv2 = EqualizedConv3d(filters_in, filters_out, 3, padding=1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2)
        self.downsampling = nn.AvgPool3d(2)
    
    def forward(self, input):
        x = self.conv1(input)
        x = self.lrelu(x)
        x = self.conv2(x)
        x = self.lrelu(x)
        x = self.downsampling(x)
        return x
        
        
class FromRGB(nn.Sequential):
    def __init__(self, channels_in, filters):
        super(FromRGB, self).__init__()
        self.fromrgb = nn.Sequential(
            EqualizedConv3d(channels_in, filters, 1),
            nn.LeakyReLU(negative_slope=0.2)
        )
    
    def forward(self, input):
        return self.fromrgb(input)

        
class MinibatchStandardDeviation(nn.Module):
    def __init__(self, group_size=4):
        super(MinibatchStandardDeviation, self).__init__()
        self.group_size = group_size
        
    def forward(self, input):
        group_size = min(self.group_size, input.shape[0])
        if group_size < len(input):
            for i in range(group_size, len(input) + 1):
                if len(input) % i == 0:
                    group_size = i
                    break
        
        s = input.shape
        y = input.view([group_size, -1, s[1], s[2], s[3], s[4]])
        y -= torch.mean(y, dim=0, keepdim=True)                     
        y = torch.mean(y ** 2, dim=0)                           
        y = torch.sqrt(y + 1e-8)
        y = torch.mean(y, dim=[1, 2, 3, 4], keepdim=True)
        y = y.repeat([group_size, 1, s[2], s[3], s[4]])
        return torch.cat([input, y], dim=1)           
    
    
class Discriminator(nn.Module):
    def __init__(self, phase, num_phases, base_dim, latent_dim, base_shape):
        super(Discriminator, self).__init__()
        self.channels = base_shape[0]
        self.base_shape = base_shape[1:]
        self.phase = phase
        
        self.fromrgbs = nn.ModuleList()        
        self.blocks = nn.ModuleList()

        for i in reversed(range(2, num_phases + 1)):
            filters_in = num_filters(i, num_phases, base_dim)
            filters_out = num_filters(i - 1, num_phases, base_dim)
            self.blocks.append(DiscriminatorBlock(filters_in, filters_out))                   
            self.fromrgbs.append(FromRGB(self.channels, filters_in))
        
        self.fromrgbs.append(FromRGB(self.channels, base_dim))

        self.downscale = nn.AvgPool3d(2)
            
        self.discriminator_out = nn.Sequential(
            MinibatchStandardDeviation(),
            EqualizedConv3d(filters_out + 1, base_dim, 3, padding=1),
            nn.LeakyReLU(negative_slope=0.2),
            nn.Flatten(),
            EqualizedLinear(np.product(self.base_shape) * base_dim, latent_dim),
            nn.LeakyReLU(negative_slope=0.2),
            EqualizedLinear(latent_dim, 1)
        )
        
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.to(self.device)
    
    def forward(self, input, alpha):
        
        input = input.to(self.device)
        
        x_downscale = input.clone()
        
        x = self.fromrgbs[-self.phase](input)
        print(x.sum(), 'fromrgb')
                        
        for i in reversed(range(1, self.phase)):
            x = self.blocks[-i](x)

            print(x.sum())
            x_downscale = self.downscale(x_downscale)
            fromrgb_prev = self.fromrgbs[-i](x_downscale)
            print(fromrgb_prev.sum(), 'fromrgb_prev')
            x = alpha * fromrgb_prev + (1 - alpha) * x
            print(x.sum(), 'x')
        x = self.discriminator_out(x)
        print('out', x.sum())
        return x
    
        
class ChannelNormalization(nn.Module):
    def __init__(self):
        super(ChannelNormalization, self).__init__()

    def forward(self, input):
        return input * torch.rsqrt(torch.mean(input ** 2, dim=1, keepdim=True) + 1e-8)

    
class GeneratorBlock(nn.Sequential):
    def __init__(self, filters_in, filters_out):
        super(GeneratorBlock, self).__init__()
        self.upsampling = nn.Upsample(scale_factor=2)
        self.conv1 = EqualizedConv3d(filters_in, filters_out, 3, padding=1)
        self.conv2 = EqualizedConv3d(filters_out, filters_out, 3, padding=1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2)
        self.cn = ChannelNormalization()
    
    def forward(self, input):
        x = self.upsampling(input)
        x = self.conv1(x)
        x = self.lrelu(x)
        x = self.cn(x)
        x = self.conv2(x)
        x = self.cn(x)
        x = self.lrelu(x)
        return x
    
class ToRGB(nn.Sequential):
    def __init__(self, filters_in, channels):
        super(ToRGB, self).__init__()
        self.conv = EqualizedConv3d(filters_in, channels, 1)
        
    def forward(self, input):
        return self.conv(input)
        
        
class Reshape(nn.Module):
    def __init__(self, shape):
        super(Reshape, self).__init__()
        self.shape = shape

    def forward(self, input):
        return torch.reshape(input, self.shape)
    
    
class Generator(nn.Module):
    def __init__(self, phase, num_phases, base_dim, latent_dim, base_shape):
        super(Generator, self).__init__()
        
        self.channels = base_shape[0]
        self.base_shape = base_shape[1:]
        self.phase = phase
        self.latent_dim = latent_dim
        filters_out = base_dim
        self.generator_in = nn.Sequential(
            EqualizedLinear(latent_dim, np.product(self.base_shape) * filters_out),
            nn.LeakyReLU(negative_slope=0.2),
            Reshape([-1, filters_out] + list(self.base_shape)),
            EqualizedConv3d(filters_out, filters_out, 3, padding=1),
            nn.LeakyReLU(negative_slope=0.2),
            ChannelNormalization(),
        )
                
        self.blocks = nn.ModuleList()
        self.to_rgbs = nn.ModuleList([ToRGB(filters_out, self.channels)])
        
        for i in range(2, num_phases + 1):
            print(f'\n\n\n phase {i}\n\n')
            filters_in = num_filters(i, num_phases, base_dim)
            filters_out = num_filters(i + 1, num_phases, base_dim)
            self.blocks.append(GeneratorBlock(filters_in, filters_out))
            self.to_rgbs.append(ToRGB(filters_out, self.channels))
        
        self.upsample = nn.Upsample(scale_factor=2)
        
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.to(self.device)
                
    def forward(self, input, alpha):
        input = input.to(self.device)
        x = self.generator_in(input)
        
        all_out = []
        
        images_out = self.to_rgbs[0](x)
        all_out.append(images_out)
        for i in range(0, self.phase - 1):
            x = self.blocks[i](x)
            img_gen = self.to_rgbs[i + 1](x)
            images_out = alpha * (self.upsample(images_out)) + (1 - alpha) * img_gen
            all_out.append(images_out)
        
        return all_out 
