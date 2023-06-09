###Source: https://github.com/xavysp/DexiNed/tree/master
from __future__ import print_function

import os
import numpy as np
import time

import cv2

import statistics

from matplotlib import pyplot as plt

import torch
import torch.nn as nn
import torch.nn.functional as F


def weight_init(m):
    if isinstance(m, (nn.Conv2d,)):
        # torch.nn.init.xavier_uniform_(m.weight, gain=1.0)
        torch.nn.init.xavier_normal_(m.weight, gain=1.0)
        # torch.nn.init.normal_(m.weight, mean=0.0, std=0.01)
        if m.weight.data.shape[1] == torch.Size([1]):
            torch.nn.init.normal_(m.weight, mean=0.0)

        if m.bias is not None:
            torch.nn.init.zeros_(m.bias)

    # for fusion layer
    if isinstance(m, (nn.ConvTranspose2d,)):
        # torch.nn.init.xavier_uniform_(m.weight, gain=1.0)
        torch.nn.init.xavier_normal_(m.weight, gain=1.0)
        # torch.nn.init.normal_(m.weight, mean=0.0, std=0.01)

        if m.weight.data.shape[1] == torch.Size([1]):
            torch.nn.init.normal_(m.weight, std=0.1)
        if m.bias is not None:
            torch.nn.init.zeros_(m.bias)


class CoFusion(nn.Module):

    def __init__(self, in_ch, out_ch):
        super(CoFusion, self).__init__()
        self.conv1 = nn.Conv2d(in_ch, 64, kernel_size=3,
                               stride=1, padding=1)
        self.conv2 = nn.Conv2d(64, 64, kernel_size=3,
                               stride=1, padding=1)
        self.conv3 = nn.Conv2d(64, out_ch, kernel_size=3,
                               stride=1, padding=1)
        self.relu = nn.ReLU()

        self.norm_layer1 = nn.GroupNorm(4, 64)
        self.norm_layer2 = nn.GroupNorm(4, 64)

    def forward(self, x):
        # fusecat = torch.cat(x, dim=1)
        attn = self.relu(self.norm_layer1(self.conv1(x)))
        attn = self.relu(self.norm_layer2(self.conv2(attn)))
        attn = F.softmax(self.conv3(attn), dim=1)

        # return ((fusecat * attn).sum(1)).unsqueeze(1)
        return ((x * attn).sum(1)).unsqueeze(1)

class _DenseLayer(nn.Sequential):
    def __init__(self, input_features, out_features):
        super(_DenseLayer, self).__init__()

        # self.add_module('relu2', nn.ReLU(inplace=True)),
        self.add_module('conv1', nn.Conv2d(input_features, out_features,
                                           kernel_size=3, stride=1, padding=2, bias=True)),
        self.add_module('norm1', nn.BatchNorm2d(out_features)),
        self.add_module('relu1', nn.ReLU(inplace=True)),
        self.add_module('conv2', nn.Conv2d(out_features, out_features,
                                           kernel_size=3, stride=1, bias=True)),
        self.add_module('norm2', nn.BatchNorm2d(out_features))

    def forward(self, x):
        x1, x2 = x

        new_features = super(_DenseLayer, self).forward(F.relu(x1))  # F.relu()
        # if new_features.shape[-1]!=x2.shape[-1]:
        #     new_features =F.interpolate(new_features,size=(x2.shape[2],x2.shape[-1]), mode='bicubic',
        #                                 align_corners=False)
        return 0.5 * (new_features + x2), x2


class _DenseBlock(nn.Sequential):
    def __init__(self, num_layers, input_features, out_features):
        super(_DenseBlock, self).__init__()
        for i in range(num_layers):
            layer = _DenseLayer(input_features, out_features)
            self.add_module('denselayer%d' % (i + 1), layer)
            input_features = out_features


class UpConvBlock(nn.Module):
    def __init__(self, in_features, up_scale):
        super(UpConvBlock, self).__init__()
        self.up_factor = 2
        self.constant_features = 16

        layers = self.make_deconv_layers(in_features, up_scale)
        assert layers is not None, layers
        self.features = nn.Sequential(*layers)

    def make_deconv_layers(self, in_features, up_scale):
        layers = []
        all_pads=[0,0,1,3,7]
        for i in range(up_scale):
            kernel_size = 2 ** up_scale
            pad = all_pads[up_scale]  # kernel_size-1
            out_features = self.compute_out_features(i, up_scale)
            layers.append(nn.Conv2d(in_features, out_features, 1))
            layers.append(nn.ReLU(inplace=True))
            layers.append(nn.ConvTranspose2d(
                out_features, out_features, kernel_size, stride=2, padding=pad))
            in_features = out_features
        return layers

    def compute_out_features(self, idx, up_scale):
        return 1 if idx == up_scale - 1 else self.constant_features

    def forward(self, x):
        return self.features(x)

class SingleConvBlock(nn.Module):
    def __init__(self, in_features, out_features, stride,
                 use_bs=True
                 ):
        super(SingleConvBlock, self).__init__()
        self.use_bn = use_bs
        self.conv = nn.Conv2d(in_features, out_features, 1, stride=stride,
                              bias=True)
        self.bn = nn.BatchNorm2d(out_features)

    def forward(self, x):
        x = self.conv(x)
        if self.use_bn:
            x = self.bn(x)
        return x


class DoubleConvBlock(nn.Module):
    def __init__(self, in_features, mid_features,
                 out_features=None,
                 stride=1,
                 use_act=True):
        super(DoubleConvBlock, self).__init__()

        self.use_act = use_act
        if out_features is None:
            out_features = mid_features
        self.conv1 = nn.Conv2d(in_features, mid_features,
                               3, padding=1, stride=stride)
        self.bn1 = nn.BatchNorm2d(mid_features)
        self.conv2 = nn.Conv2d(mid_features, out_features, 3, padding=1)
        self.bn2 = nn.BatchNorm2d(out_features)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.conv2(x)
        x = self.bn2(x)
        if self.use_act:
            x = self.relu(x)
        return x


class DexiNed(nn.Module):
    """ Definition of the DXtrem network. """

    def __init__(self):
        super(DexiNed, self).__init__()
        self.block_1 = DoubleConvBlock(3, 32, 64, stride=2,)
        self.block_2 = DoubleConvBlock(64, 128, use_act=False)
        self.dblock_3 = _DenseBlock(2, 128, 256) # [128,256,100,100]
        self.dblock_4 = _DenseBlock(3, 256, 512)
        self.dblock_5 = _DenseBlock(3, 512, 512)
        self.dblock_6 = _DenseBlock(3, 512, 256)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)

        # left skip connections, figure in Journal
        self.side_1 = SingleConvBlock(64, 128, 2)
        self.side_2 = SingleConvBlock(128, 256, 2)
        self.side_3 = SingleConvBlock(256, 512, 2)
        self.side_4 = SingleConvBlock(512, 512, 1)
        self.side_5 = SingleConvBlock(512, 256, 1) # Sory I forget to comment this line :(

        # right skip connections, figure in Journal paper
        self.pre_dense_2 = SingleConvBlock(128, 256, 2)
        self.pre_dense_3 = SingleConvBlock(128, 256, 1)
        self.pre_dense_4 = SingleConvBlock(256, 512, 1)
        self.pre_dense_5 = SingleConvBlock(512, 512, 1)
        self.pre_dense_6 = SingleConvBlock(512, 256, 1)


        self.up_block_1 = UpConvBlock(64, 1)
        self.up_block_2 = UpConvBlock(128, 1)
        self.up_block_3 = UpConvBlock(256, 2)
        self.up_block_4 = UpConvBlock(512, 3)
        self.up_block_5 = UpConvBlock(512, 4)
        self.up_block_6 = UpConvBlock(256, 4)
        self.block_cat = SingleConvBlock(6, 1, stride=1, use_bs=False) # hed fusion method
        # self.block_cat = CoFusion(6,6)# cats fusion method


        self.apply(weight_init)

    def slice(self, tensor, slice_shape):
        t_shape = tensor.shape
        height, width = slice_shape
        if t_shape[-1]!=slice_shape[-1]:
            new_tensor = F.interpolate(
                tensor, size=(height, width), mode='bicubic',align_corners=False)
        else:
            new_tensor=tensor
        # tensor[..., :height, :width]
        return new_tensor

    def forward(self, x):
        assert x.ndim == 4, x.shape

        # Block 1
        block_1 = self.block_1(x)
        block_1_side = self.side_1(block_1)

        # Block 2
        block_2 = self.block_2(block_1)
        block_2_down = self.maxpool(block_2)
        block_2_add = block_2_down + block_1_side
        block_2_side = self.side_2(block_2_add)

        # Block 3
        block_3_pre_dense = self.pre_dense_3(block_2_down)
        block_3, _ = self.dblock_3([block_2_add, block_3_pre_dense])
        block_3_down = self.maxpool(block_3) # [128,256,50,50]
        block_3_add = block_3_down + block_2_side
        block_3_side = self.side_3(block_3_add)

        # Block 4
        block_2_resize_half = self.pre_dense_2(block_2_down)
        block_4_pre_dense = self.pre_dense_4(block_3_down+block_2_resize_half)
        block_4, _ = self.dblock_4([block_3_add, block_4_pre_dense])
        block_4_down = self.maxpool(block_4)
        block_4_add = block_4_down + block_3_side
        block_4_side = self.side_4(block_4_add)

        # Block 5
        block_5_pre_dense = self.pre_dense_5(
            block_4_down) #block_5_pre_dense_512 +block_4_down
        block_5, _ = self.dblock_5([block_4_add, block_5_pre_dense])
        block_5_add = block_5 + block_4_side

        # Block 6
        block_6_pre_dense = self.pre_dense_6(block_5)
        block_6, _ = self.dblock_6([block_5_add, block_6_pre_dense])

        # upsampling blocks
        out_1 = self.up_block_1(block_1)
        out_2 = self.up_block_2(block_2)
        out_3 = self.up_block_3(block_3)
        out_4 = self.up_block_4(block_4)
        out_5 = self.up_block_5(block_5)
        out_6 = self.up_block_6(block_6)
        results = [out_1, out_2, out_3, out_4, out_5, out_6]

        # concatenate multiscale outputs
        block_cat = torch.cat(results, dim=1)  # Bx6xHxW
        block_cat = self.block_cat(block_cat)  # Bx1xHxW

        # return results
        results.append(block_cat)
        return results


class Deep_Edge_Detector():
    #load weights and init model
    def __init__(self, checkpoint_path) -> None:
        self.device = torch.device('cpu' if torch.cuda.device_count() == 0
                            else 'cuda')

        if not os.path.isfile(checkpoint_path):
            raise FileNotFoundError(
                f"Checkpoint filte note found: {checkpoint_path}")
        print(f"loaded weights from: {checkpoint_path}")
        self.model = DexiNed().to(device)
        self.model.load_state_dict(torch.load(checkpoint_path, map_location=device))

        # Put model in evaluation mode
        self.model.eval()


    def predict(self, image : np.array) -> np.array:
        output = self.model(torch.from_numpy(image.T).unsqueeze(0).to(self.device))
        image = output[-1].squeeze().detach().cpu().numpy().T
        return image
    
    def get_nonEdges(self, inp_image: np.array) -> np.array:
        output = self.model(torch.from_numpy(inp_image.T).unsqueeze(0).to(self.device))
        img = output[-1]

        th_img = img <= 0
        th_img = th_img.squeeze().detach().cpu().numpy().astype(np.uint8).T

        return th_img

def convert_gray_to_HSV(depth_image):
    # Normalize the depth image to fall within the range 0-179 (to fit the hue range in HSV color space)
    depth_image = depth_image[:,:,0]
    # Normalize the depth image to fall within the range 0-179 (to fit the hue range in HSV color space)
    normalized_depth_image = cv2.normalize(depth_image, None, 0, 179, cv2.NORM_MINMAX, dtype=cv2.CV_8U)

    # Convert the single channel image to three channels
    three_channel_depth_image = cv2.cvtColor(normalized_depth_image, cv2.COLOR_GRAY2BGR)

    # Convert the three channel image to HSV
    hsv_image = cv2.cvtColor(three_channel_depth_image, cv2.COLOR_BGR2HSV)

    # Replace the hue channel with the depth image and set saturation and value to maximum
    hsv_image[:, :, 0] = normalized_depth_image  # Hue
    hsv_image[:, :, 1] = 255  # Saturation
    hsv_image[:, :, 2] = 255  # Value

    # Convert back to BGR
    colored_depth_image = cv2.cvtColor(hsv_image, cv2.COLOR_HSV2BGR)
    # Save the colored depth image
    cv2.imwrite("colored_depth_image.png", colored_depth_image)

    return colored_depth_image.astype(np.float32)
def normalize(image):
    image = (image - np.min(image)) / np.max(image) * 255
    return image

if __name__ == '__main__':
    device = torch.device('cpu' if torch.cuda.device_count() == 0
                          else 'cuda')

    checkpoint_path = "checkpoint/10_model.pth"
    imagepath = 'dataset/depth_Images_normalized/000938.png' #

    num_repetitions = 20 #min 10 itterations

    # Get image, convert to float
    image = cv2.imread(imagepath)
    image = image.astype(np.float32)
    image = (image - np.min(image)) / np.max(image) * 255

    rgb_image = cv2.imread('000938.png')
    rgb_image = rgb_image.astype(np.float32)
    rgb_image = normalize(rgb_image)
    
    # image = convert_gray_to_HSV(image)

    detectron_2000 = Deep_Edge_Detector(checkpoint_path)
    start = time.time()
    dexined_time = []
    for i in range(num_repetitions):
        start_1 = time.time()
        edges = detectron_2000.get_nonEdges(rgb_image) 
        dexined_time.append(time.time()- start_1)
    dexined_time_stable= dexined_time[10: -1]
    dexined_mean = statistics.mean(dexined_time_stable)
    print(f"DexiNed time per iteration: {dexined_mean}")

    
    image = image[:,:,0]
    canny_image = image.astype(np.uint8)

    start = time.time()
    for i in range(10):
        edges_canny = cv2.Canny(canny_image, 5, 100)
    print(f"Canny: {(time.time() - start)/10}")

    # plt.subplot(121),plt.imshow(canny_image,cmap = 'gray')
    # plt.title('Original Image'), plt.xticks([]), plt.yticks([])
    # plt.subplot(122),plt.imshow(edges_canny,cmap = 'gray')
    # plt.title('Edge Image'), plt.xticks([]), plt.yticks([])
    # plt.show()


    thresholdValue = 100
    start = time.time()
    for i in range(1000):
        thresholdmask = (image <= thresholdValue).astype(np.uint8)
    print(f"dynamic treshold: {(time.time() - start)/1000}")
    masked_image = thresholdmask * edges
    image_thresholded = (255 - image) * thresholdmask
    image_thresholded = normalize(image_thresholded)
    resulting = (255 - image) * masked_image
    
    cv2.imwrite('masked_depth.png', masked_image *255)
    cv2.imwrite('image_thresholded.png', image_thresholded)
    cv2.imwrite('mask.png', thresholdmask*255)
    cv2.imwrite('resulting.png', resulting)
    cv2.imwrite('edges.png', edges * 255)
    cv2.imwrite('canny.png', edges_canny)