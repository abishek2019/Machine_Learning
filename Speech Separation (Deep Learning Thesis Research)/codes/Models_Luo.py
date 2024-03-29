import numpy as np
import os
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

class DepthConv1d(nn.Module):

    def __init__(self, input_channel, hidden_channel, kernel, padding, dilation=1, skip=True, causal=False):
        super(DepthConv1d, self).__init__()
        self.causal = causal
        self.skip = skip
        self.conv1d = nn.Conv1d(input_channel, hidden_channel, 1)
        if self.causal:
            self.padding = (kernel - 1) * dilation
        else:
            self.padding = padding
        self.dconv1d = nn.Conv1d(hidden_channel, hidden_channel, kernel, dilation=dilation, groups=hidden_channel, padding=self.padding)
        self.res_out = nn.Conv1d(hidden_channel, input_channel, 1)
        self.nonlinearity1 = nn.PReLU()
        self.nonlinearity2 = nn.PReLU()
        self.reg1 = nn.GroupNorm(1, hidden_channel, eps=1e-08)
        self.reg2 = nn.GroupNorm(1, hidden_channel, eps=1e-08)
        if self.skip:
            self.skip_out = nn.Conv1d(hidden_channel, input_channel, 1)

    def forward(self, input):
        output = self.reg1(self.nonlinearity1(self.conv1d(input)))
        output = self.reg2(self.nonlinearity2(self.dconv1d(output)))
        residual = self.res_out(output)
        if self.skip:
            skip = self.skip_out(output)
            return residual, skip
        else:
            return residual


class TCN(nn.Module):
    def __init__(self, input_dim, output_dim, BN_dim, hidden_dim, layer, stack, kernel=3, skip=True, causal=False, dilated=True):
        super(TCN, self).__init__()
        # input is a sequence of features of shape (B, N, L)
        # normalization
        self.LN = nn.GroupNorm(1, input_dim, eps=1e-8)
        self.BN = nn.Conv1d(input_dim, BN_dim, 1)
        # TCN for feature extraction
        self.receptive_field = 0
        self.dilated = dilated
        self.TCN = nn.ModuleList([])
        for s in range(stack):
            for i in range(layer):
                if self.dilated:
                    self.TCN.append(DepthConv1d(BN_dim, hidden_dim, kernel, dilation=2 ** i, padding=2 ** i, skip=skip, causal=causal))
                else:
                    self.TCN.append(DepthConv1d(BN_dim, hidden_dim, kernel, dilation=1, padding=1, skip=skip, causal=causal))
                if i == 0 and s == 0:
                    self.receptive_field += kernel
                else:
                    if self.dilated:
                        self.receptive_field += (kernel - 1) * 2 ** i
                    else:
                        self.receptive_field += (kernel - 1)
        # print("Receptive field: {:3d} frames.".format(self.receptive_field))
        # output layer
        self.output = nn.Sequential(nn.PReLU(), nn.Conv1d(BN_dim, output_dim, 1))
        self.skip = skip

    def forward(self, input):
        # input shape: (B, N, L)
        # normalization
        output = self.BN(self.LN(input))
        # pass to TCN
        if self.skip:
            skip_connection = 0.
            for i in range(len(self.TCN)):
                residual, skip = self.TCN[i](output)
                output = output + residual
                skip_connection = skip_connection + skip
        else:
            for i in range(len(self.TCN)):
                residual = self.TCN[i](output)
                output = output + residual
        # output layer
        if self.skip:
            output = self.output(skip_connection)
        else:
            output = self.output(output)
        return output

# Conv-TasNet
class TasNet(nn.Module):
    def __init__(self, enc_dim=512, feature_dim=128, sr=16000, win=2, layer=8, stack=3, kernel=3, num_spk=2, causal=False):
        super(TasNet, self).__init__()
        # hyper parameters
        self.num_spk = num_spk
        self.enc_dim = enc_dim
        self.feature_dim = feature_dim
        self.win = int(sr * win / 1000)     #org 16
        self.stride = self.win // 2         #org 8
        self.layer = layer
        self.stack = stack
        self.kernel = kernel
        self.causal = causal
        # input encoder
        self.encoder = nn.Conv1d(1, self.enc_dim, self.win, bias=False, stride=self.stride)
        # TCN separator
        self.TCN = TCN(self.enc_dim, self.enc_dim * self.num_spk, self.feature_dim, self.feature_dim * 4,
                              self.layer, self.stack, self.kernel, causal=self.causal)
        self.receptive_field = self.TCN.receptive_field
        # output decoder
        self.decoder = nn.ConvTranspose1d(self.enc_dim, 1, self.win, bias=False, stride=self.stride)

    def pad_signal(self, input):

        # input is the waveforms: (B, T) or (B, 1, T)
        # reshape and padding
        if input.dim() not in [2, 3]:
            raise RuntimeError("Input can only be 2 or 3 dimensional.")
        if input.dim() == 2:
            input = input.unsqueeze(1)
        batch_size = input.size(0)
        nsample = input.size(2)
        rest = self.win - (self.stride + nsample % self.win) % self.win
        if rest > 0:
            pad = Variable(torch.zeros(batch_size, 1, rest)).type(input.type())
            input = torch.cat([input, pad], 2)
        pad_aux = Variable(torch.zeros(batch_size, 1, self.stride)).type(input.type())
        input = torch.cat([pad_aux, input, pad_aux], 2)
        return input, rest

    def forward(self, input):
        # padding
        output, rest = self.pad_signal(input)
        batch_size = output.size(0)
        # waveform encoder
        enc_output = self.encoder(output)  # B, N, L
        # generate masks
        masks = torch.sigmoid(self.TCN(enc_output)).view(batch_size, self.num_spk, self.enc_dim, -1)  # B, C, N, L
        masked_output = enc_output.unsqueeze(1) * masks  # B, C, N, L
        # waveform decoder
        output = self.decoder(masked_output.view(batch_size * self.num_spk, self.enc_dim, -1))  # B*C, 1, L
        output = output[:, :, self.stride:-(rest + self.stride)].contiguous()  # B*C, 1, L
        output = output.view(batch_size, self.num_spk, -1)  # B, C, T
        return output


def test_conv_tasnet():
    x = torch.rand(2, 32000)
    nnet = TasNet()
    x = nnet(x)
    s1 = x[0]
    print(s1.shape)


if __name__ == "__main__":
    test_conv_tasnet()
