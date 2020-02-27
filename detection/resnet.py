from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from paddle import fluid
from paddle.fluid.param_attr import ParamAttr
from paddle.fluid.regularizer import L2Decay
from paddle.fluid.initializer import Constant


class ResNet(object):
    def __init__(self,
                 depth=50,
                 freeze_at=2,
                 norm_type='affine_channel',
                 freeze_norm=True,
                 norm_decay=0.,
                 variant='b',
                 feature_maps=[2, 3, 4, 5],
                 dcn_v2_stages=[],
                 weight_prefix_name=''):
        super(ResNet, self).__init__()

        assert depth in [18, 34, 50, 101, 152, 200], \
            "depth {} not in [18, 34, 50, 101, 152, 200]"
        assert variant in ['a', 'b', 'c', 'd'], "invalid ResNet variant"
        assert 0 <= freeze_at <= 4, "freeze_at should be 0, 1, 2, 3 or 4"
        assert len(feature_maps) > 0, "need one or more feature maps"
        assert norm_type in ['bn', 'sync_bn', 'affine_channel']

        self.depth = depth
        self.freeze_at = freeze_at
        self.norm_type = norm_type
        self.norm_decay = norm_decay
        self.freeze_norm = freeze_norm
        self.variant = variant
        self._model_type = 'ResNet'
        self.feature_maps = feature_maps
        self.dcn_v2_stages = dcn_v2_stages
        self.depth_cfg = {
            18: ([2, 2, 2, 2], self.basicblock),
            34: ([3, 4, 6, 3], self.basicblock),
            50: ([3, 4, 6, 3], self.bottleneck),
            101: ([3, 4, 23, 3], self.bottleneck),
            152: ([3, 8, 36, 3], self.bottleneck),
            200: ([3, 12, 48, 3], self.bottleneck),
        }
        self.stage_filters = [64, 128, 256, 512]
        self._c1_out_chan_num = 64
        self.prefix_name = weight_prefix_name

    def _conv_offset(self,
                     input,
                     filter_size,
                     stride,
                     padding,
                     act=None,
                     name=None):
        out_channel = filter_size * filter_size * 3
        out = fluid.layers.conv2d(
            input,
            num_filters=out_channel,
            filter_size=filter_size,
            stride=stride,
            padding=padding,
            param_attr=ParamAttr(
                initializer=Constant(0.0), name=name + ".w_0"),
            bias_attr=ParamAttr(
                initializer=Constant(0.0), name=name + ".b_0"),
            act=act,
            name=name)
        return out

    def _conv_norm(self,
                   input,
                   num_filters,
                   filter_size,
                   stride=1,
                   groups=1,
                   act=None,
                   name=None,
                   dcn_v2=False):
        _name = self.prefix_name + name if self.prefix_name != '' else name
        if not dcn_v2:
            conv = fluid.layers.conv2d(
                input=input,
                num_filters=num_filters,
                filter_size=filter_size,
                stride=stride,
                padding=(filter_size - 1) // 2,
                groups=groups,
                act=None,
                param_attr=ParamAttr(name=_name + "_weights"),
                bias_attr=False,
                name=_name + '.conv2d.output.1')
        else:
            # select deformable conv"
            offset_mask = self._conv_offset(
                input=input,
                filter_size=filter_size,
                stride=stride,
                padding=(filter_size - 1) // 2,
                act=None,
                name=_name + "_conv_offset")
            offset_channel = filter_size**2 * 2
            mask_channel = filter_size**2
            offset, mask = fluid.layers.split(
                input=offset_mask,
                num_or_sections=[offset_channel, mask_channel],
                dim=1)
            mask = fluid.layers.sigmoid(mask)
            conv = fluid.layers.deformable_conv(
                input=input,
                offset=offset,
                mask=mask,
                num_filters=num_filters,
                filter_size=filter_size,
                stride=stride,
                padding=(filter_size - 1) // 2,
                groups=groups,
                deformable_groups=1,
                im2col_step=1,
                param_attr=ParamAttr(name=_name + "_weights"),
                bias_attr=False,
                name=_name + ".conv2d.output.1")

        bn_name = "bn_" + name if name == "conv1" else "bn" + name[3:]
        bn_name = self.prefix_name + bn_name if self.prefix_name != '' else bn_name

        norm_lr = 0. if self.freeze_norm else 1.
        norm_decay = self.norm_decay
        pattr = ParamAttr(
            name=bn_name + '_scale',
            learning_rate=norm_lr,
            regularizer=L2Decay(norm_decay))
        battr = ParamAttr(
            name=bn_name + '_offset',
            learning_rate=norm_lr,
            regularizer=L2Decay(norm_decay))

        if self.norm_type in ['bn', 'sync_bn']:
            global_stats = True if self.freeze_norm else False
            out = fluid.layers.batch_norm(
                input=conv,
                act=act,
                name=bn_name + '.output.1',
                param_attr=pattr,
                bias_attr=battr,
                moving_mean_name=bn_name + '_mean',
                moving_variance_name=bn_name + '_variance',
                use_global_stats=global_stats)
            scale = fluid.framework._get_var(pattr.name)
            bias = fluid.framework._get_var(battr.name)
        elif self.norm_type == 'affine_channel':
            scale = fluid.layers.create_parameter(
                shape=[conv.shape[1]],
                dtype=conv.dtype,
                attr=pattr,
                default_initializer=fluid.initializer.Constant(1.))
            bias = fluid.layers.create_parameter(
                shape=[conv.shape[1]],
                dtype=conv.dtype,
                attr=battr,
                default_initializer=fluid.initializer.Constant(0.))
            out = fluid.layers.affine_channel(
                x=conv, scale=scale, bias=bias, act=act)
        if self.freeze_norm:
            scale.stop_gradient = True
            bias.stop_gradient = True
        return out

    def _shortcut(self, input, ch_out, stride, is_first, name):
        max_pooling_in_short_cut = self.variant == 'd'
        ch_in = input.shape[1]
        # the naming rule is same as pretrained weight
        std_senet = getattr(self, 'std_senet', False)
        if ch_in != ch_out or stride != 1 or (self.depth < 50 and is_first):
            if std_senet:
                if is_first:
                    return self._conv_norm(input, ch_out, 1, stride, name=name)
                else:
                    return self._conv_norm(input, ch_out, 3, stride, name=name)
            if max_pooling_in_short_cut and not is_first:
                input = fluid.layers.pool2d(
                    input=input,
                    pool_size=2,
                    pool_stride=2,
                    pool_padding=0,
                    ceil_mode=True,
                    pool_type='avg')
                return self._conv_norm(input, ch_out, 1, 1, name=name)
            return self._conv_norm(input, ch_out, 1, stride, name=name)
        else:
            return input

    def bottleneck(self,
                   input,
                   num_filters,
                   stride,
                   is_first,
                   name,
                   dcn_v2=False):
        if self.variant == 'a':
            stride1, stride2 = stride, 1
        else:
            stride1, stride2 = 1, stride

        # ResNeXt
        groups = getattr(self, 'groups', 1)
        group_width = getattr(self, 'group_width', -1)
        if groups == 1:
            expand = 4
        elif (groups * group_width) == 256:
            expand = 1
        else:  # FIXME hard code for now, handles 32x4d, 64x4d and 32x8d
            num_filters = num_filters // 2
            expand = 2

        conv_name1 = name + "_branch2a"
        conv_name2 = name + "_branch2b"
        conv_name3 = name + "_branch2c"
        shortcut_name = name + "_branch1"
        std_senet = getattr(self, 'std_senet', False)
        if std_senet:
            conv_def = [
                [int(num_filters / 2), 1, stride1, 'relu', 1, conv_name1],
                [num_filters, 3, stride2, 'relu', groups, conv_name2],
                [num_filters * expand, 1, 1, None, 1, conv_name3]
            ]
        else:
            conv_def = [[num_filters, 1, stride1, 'relu', 1, conv_name1],
                        [num_filters, 3, stride2, 'relu', groups, conv_name2],
                        [num_filters * expand, 1, 1, None, 1, conv_name3]]

        residual = input
        for i, (c, k, s, act, g, _name) in enumerate(conv_def):
            residual = self._conv_norm(
                input=residual,
                num_filters=c,
                filter_size=k,
                stride=s,
                act=act,
                groups=g,
                name=_name,
                dcn_v2=(i == 1 and dcn_v2))
        short = self._shortcut(
            input,
            num_filters * expand,
            stride,
            is_first=is_first,
            name=shortcut_name)
        # Squeeze-and-Excitation
        if callable(getattr(self, '_squeeze_excitation', None)):
            residual = self._squeeze_excitation(
                input=residual, num_channels=num_filters, name='fc' + name)
        return fluid.layers.elementwise_add(
            x=short, y=residual, act='relu', name=name + ".add.output.5")

    def basicblock(self,
                   input,
                   num_filters,
                   stride,
                   is_first,
                   name,
                   dcn_v2=False):
        assert dcn_v2 is False, "Not implemented yet."
        conv0 = self._conv_norm(
            input=input,
            num_filters=num_filters,
            filter_size=3,
            act='relu',
            stride=stride,
            name=name + "_branch2a")
        conv1 = self._conv_norm(
            input=conv0,
            num_filters=num_filters,
            filter_size=3,
            act=None,
            name=name + "_branch2b")
        short = self._shortcut(
            input, num_filters, stride, is_first, name=name + "_branch1")
        return fluid.layers.elementwise_add(x=short, y=conv1, act='relu')

    def layer_warp(self, input, stage_num):
        """
        Args:
            input (Variable): input variable.
            stage_num (int): the stage number, should be 2, 3, 4, 5
        Returns:
            The last variable in endpoint-th stage.
        """
        assert stage_num in [2, 3, 4, 5]

        stages, block_func = self.depth_cfg[self.depth]
        count = stages[stage_num - 2]

        ch_out = self.stage_filters[stage_num - 2]
        is_first = False if stage_num != 2 else True
        dcn_v2 = True if stage_num in self.dcn_v2_stages else False

        # Make the layer name and parameter name consistent
        # with ImageNet pre-trained model
        conv = input
        for i in range(count):
            name = 'res' + str(stage_num)
            if count > 10 and stage_num == 4:
                if i == 0:
                    conv_name = name + "a"
                else:
                    conv_name = name + "b" + str(i)
            else:
                conv_name = name + chr(ord("a") + i)

            if self.depth < 50:
                is_first = True if i == 0 and stage_num == 2 else False
            conv = block_func(
                input=conv,
                num_filters=ch_out,
                stride=2 if i == 0 and stage_num != 2 else 1,
                is_first=is_first,
                name=conv_name,
                dcn_v2=dcn_v2)
        return conv

    def c1_stage(self, input):
        out_chan = self._c1_out_chan_num

        conv1_name = "conv1"

        if self.variant in ['c', 'd']:
            conv_def = [
                [out_chan // 2, 3, 2, "conv1_1"],
                [out_chan // 2, 3, 1, "conv1_2"],
                [out_chan, 3, 1, "conv1_3"],
            ]
        else:
            conv_def = [[out_chan, 7, 2, conv1_name]]

        for (c, k, s, _name) in conv_def:
            input = self._conv_norm(
                input=input,
                num_filters=c,
                filter_size=k,
                stride=s,
                act='relu',
                name=_name)

        output = fluid.layers.pool2d(
            input=input,
            pool_size=3,
            pool_stride=2,
            pool_padding=1,
            pool_type='max')
        return output

    def __call__(self, input):
        res_endpoints = []

        res = input
        feature_maps = self.feature_maps
        severed_head = getattr(self, 'severed_head', False)
        if not severed_head:
            res = self.c1_stage(res)
            feature_maps = range(2, max(self.feature_maps) + 1)

        for i in feature_maps:
            res = self.layer_warp(res, i)
            if i in self.feature_maps:
                res_endpoints.append(res)
            if self.freeze_at == i:
                res.stop_gradient = True

        return res_endpoints
