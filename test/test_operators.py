from test_pytorch_common import TestCase, run_tests, skipIfNoLapack, flatten
import test_onnx_common

import torch
import torch.onnx
from torch.autograd import Variable, Function, NestedIOFunction
from torch.autograd.function import symbolic_override
from torch.nn import Module
import torch.nn as nn

import onnx
import onnx.checker
import onnx.helper

import google.protobuf.text_format

import itertools
import io
import unittest
import inspect
import argparse
import glob
import os
import shutil
import sys
import common
from onnx import numpy_helper


_onnx_test = False


def export_to_string(model, inputs, *args, **kwargs):
    f = io.BytesIO()
    torch.onnx.export(model, inputs, f, *args, **kwargs)
    return f.getvalue()


class FuncModule(Module):
    def __init__(self, f, params=tuple()):
        super(FuncModule, self).__init__()
        self.f = f
        self.params = nn.ParameterList(list(params))

    def forward(self, *args):
        return self.f(*itertools.chain(args, self.params))


class TestOperators(TestCase):

    def assertONNXExpected(self, binary_pb, subname=None):
        model_def = onnx.ModelProto.FromString(binary_pb)
        onnx.checker.check_model(model_def)
        # doc_string contains stack trace in it, strip it
        onnx.helper.strip_doc_string(model_def)
        self.assertExpected(google.protobuf.text_format.MessageToString(model_def, float_format='.15g'), subname)

    def assertONNX(self, f, args, params=tuple(), **kwargs):
        if isinstance(f, nn.Module):
            m = f
        else:
            m = FuncModule(f, params)
        onnx_model_pb = export_to_string(m, args, **kwargs)
        self.assertONNXExpected(onnx_model_pb)
        if _onnx_test:
            test_function = inspect.stack()[1][0].f_code.co_name
            test_name = test_function[0:4] + "_operator" + test_function[4:]
            output_dir = test_onnx_common.output_dir(test_name)
            if os.path.exists(output_dir):
                shutil.rmtree(output_dir)
            os.makedirs(output_dir)
            with open(os.path.join(output_dir, "model.pb"), 'wb') as file:
                file.write(onnx_model_pb)
            data_dir = os.path.join(output_dir, "test_data_set_0")
            os.makedirs(data_dir)
            if isinstance(args, Variable):
                args = (args,)
            for index, var in enumerate(flatten(args)):
                tensor = numpy_helper.from_array(var.data.numpy())
                with open(os.path.join(data_dir, "input_{}.pb".format(index)), 'wb') as file:
                    file.write(tensor.SerializeToString())
            outputs = m(*args)
            if isinstance(outputs, Variable):
                outputs = (outputs,)
            for index, var in enumerate(flatten(outputs)):
                tesnor = numpy_helper.from_array(var.data.numpy())
                with open(os.path.join(data_dir, "output_{}.pb".format(index)), 'wb') as file:
                    file.write(tensor.SerializeToString())

    def assertONNXRaises(self, err, f, args, params=tuple(), **kwargs):
        if isinstance(f, nn.Module):
            m = f
        else:
            m = FuncModule(f, params)
        self.assertExpectedRaises(err, lambda: export_to_string(m, args, **kwargs))

    def test_basic(self):
        x = Variable(torch.Tensor([0.4]), requires_grad=True)
        y = Variable(torch.Tensor([0.7]), requires_grad=True)
        self.assertONNX(lambda x, y: -torch.sigmoid(torch.tanh(x * (x + y))), (x, y))

    def test_view(self):
        x = Variable(torch.Tensor([0]), requires_grad=True)
        self.assertONNX(lambda x: x.view(1, 1), x)

    @unittest.skip("Indexing is broken by #3725")
    def test_index(self):
        x = Variable(torch.Tensor([[0]]), requires_grad=True)
        self.assertONNX(lambda x: x[0], x)

    def test_addconstant(self):
        x = Variable(torch.DoubleTensor(2, 3), requires_grad=True)
        self.assertONNX(lambda x: x + 1, x)

    def test_add_broadcast(self):
        x = Variable(torch.DoubleTensor(2, 3), requires_grad=True)
        y = Variable(torch.DoubleTensor(3), requires_grad=True)
        self.assertONNX(lambda x, y: x + y, (x, y))

    def test_add_left_broadcast(self):
        x = Variable(torch.DoubleTensor(3), requires_grad=True)
        y = Variable(torch.DoubleTensor(2, 3), requires_grad=True)
        self.assertONNXRaises(RuntimeError, lambda x, y: x + y, (x, y))

    def test_add_size1_broadcast(self):
        x = Variable(torch.DoubleTensor(2, 3), requires_grad=True)
        y = Variable(torch.DoubleTensor(2, 1), requires_grad=True)
        self.assertONNXRaises(RuntimeError, lambda x, y: x + y, (x, y))

    def test_transpose(self):
        x = Variable(torch.Tensor([[0, 1], [2, 3]]), requires_grad=True)
        self.assertONNX(lambda x: x.transpose(0, 1).transpose(1, 0), x)

    def test_chunk(self):
        x = Variable(torch.Tensor([0,1,2]), requires_grad=True)
        self.assertONNX(lambda x: x.chunk(2), x)

    def test_concat2(self):
        # volatile is of particular interest; it caused a segfault
        # with the exporter
        x = Variable(torch.randn(2, 3), volatile=True)
        y = Variable(torch.randn(2, 3), volatile=True)
        self.assertONNX(lambda inputs: torch.cat(inputs, 1), ((x, y),))

    def test_mm(self):
        m1 = Variable(torch.randn(2, 3), requires_grad=True)
        m2 = Variable(torch.randn(3, 4), requires_grad=True)
        self.assertONNX(torch.mm, (m1, m2))

    def test_addmm(self):
        m1 = Variable(torch.randn(2, 3), requires_grad=True)
        m2 = Variable(torch.randn(3, 4), requires_grad=True)
        m3 = Variable(torch.randn(4), requires_grad=True)
        self.assertONNX(lambda x, y, z: torch.addmm(torch.addmm(z, x, y), x, y), (m1, m2, m3))

    def test_permute2(self):
        x = Variable(torch.Tensor([[[[[[0]]]]]]), requires_grad=True)
        self.assertONNX(lambda x: x.permute(0, 1, 4, 2, 5, 3), x)

    def test_pad(self):
        x = Variable(torch.Tensor([[[[0, 1, 1, 1], [2, 3, 7, 7]]]]), requires_grad=True)
        self.assertONNX(nn.ReflectionPad2d((3, 4, 1, 2)), x)

    def test_params(self):
        x = Variable(torch.Tensor([[1, 2], [3, 4]]), requires_grad=True)
        y = nn.Parameter(torch.Tensor([[1, 2], [3, 4]]), requires_grad=True)
        self.assertONNX(lambda x, y: -torch.sigmoid(torch.tanh(x * (x + y))), x, params=(y, ))

    def test_non_float_params(self):
        x = Variable(torch.LongTensor([[1, 2], [3, 4]]), requires_grad=True)
        y = nn.Parameter(torch.LongTensor([[1, 2], [3, 4]]), requires_grad=True)
        self.assertONNX(lambda x, y: x * (x + y), x, params=(y, ))

    def test_symbolic_mismatch(self):
        class MyFun(Function):
            @staticmethod
            def symbolic(g, x):
                # The inside of this function should never be invoked, because
                # we will fail due to an argument mismatch first.
                assert False

            @staticmethod
            def forward(ctx, x, y):
                return x + y

        x = Variable(torch.randn(2, 2).fill_(1.0))
        y = Variable(torch.randn(2, 2).fill_(1.0))
        # NB: Don't use expect test here, the type error wobbles depending
        # on Python version
        with self.assertRaisesRegex(TypeError, "occurred when translating MyFun"):
            export_to_string(FuncModule(MyFun().apply), (x, y))

    # TODO: Do an nn style test for these
    def test_batchnorm(self):
        x = Variable(torch.randn(2, 2).fill_(1.0), requires_grad=True)
        self.assertONNX(nn.BatchNorm2d(2), x)

    def test_batchnorm_training(self):
        x = Variable(torch.randn(2, 2).fill_(1.0), requires_grad=True)
        self.assertONNX(nn.BatchNorm2d(2), x, training=True)

    def test_conv(self):
        x = Variable(torch.randn(20, 16, 50, 40).fill_(1.0), requires_grad=True)
        self.assertONNX(nn.Conv2d(16, 13, 3, bias=False), x)

    def test_maxpool(self):
        x = Variable(torch.randn(20, 16, 50))
        self.assertONNX(nn.MaxPool1d(3, stride=2), x)

    @unittest.skip('Broken by onnx repo Variadic inputs commit '
                   'https://github.com/onnx/onnx/commit/fd5493013e6cd1f41a560f05965216ec106f56eb')
    def test_at_op(self):
        x = Variable(torch.randn(3, 4))

        class MyFun(Function):

            @staticmethod
            def symbolic(g, x):
                return g.at("add", x, x)

            @staticmethod
            def forward(ctx, x):
                return x + x

        class MyModule(Module):
            def forward(self, x):
                return MyFun.apply(x)

        self.assertONNX(MyModule(), x)

    def test_symbolic_override(self):
        """Lifted from fast-neural-style: custom implementation of instance norm
        to be mapped to ONNX operator"""

        class CustomInstanceNorm(torch.nn.Module):
            def __init__(self, dim, eps=1e-9):
                super(CustomInstanceNorm, self).__init__()
                self.scale = nn.Parameter(torch.FloatTensor(dim).uniform_())
                self.shift = nn.Parameter(torch.FloatTensor(dim).zero_())
                self.eps = eps

            def forward(self, x):
                return self._run_forward(x, self.scale, self.shift, eps=self.eps)

            @staticmethod
            @symbolic_override(
                lambda g, x, scale, shift, eps: g.op(
                    'InstanceNormalization', x, scale, shift, epsilon_f=eps)
            )
            def _run_forward(x, scale, shift, eps):
                # since we hand-roll instance norm it doesn't perform well all in fp16
                n = x.size(2) * x.size(3)
                t = x.view(x.size(0), x.size(1), n)
                mean = torch.mean(t, 2).unsqueeze(2).unsqueeze(3).expand_as(x)
                # Calculate the biased var. torch.var returns unbiased var
                var = torch.var(t, 2).unsqueeze(2).unsqueeze(3).expand_as(x) * ((n - 1) / float(n))
                scale_broadcast = scale.unsqueeze(1).unsqueeze(1).unsqueeze(0)
                scale_broadcast = scale_broadcast.expand_as(x)
                shift_broadcast = shift.unsqueeze(1).unsqueeze(1).unsqueeze(0)
                shift_broadcast = shift_broadcast.expand_as(x)
                out = (x - mean) / torch.sqrt(var + eps)
                out = out * scale_broadcast + shift_broadcast
                return out

        instnorm = CustomInstanceNorm(10)
        x = Variable(torch.randn(2, 10, 32, 32))
        self.assertONNX(instnorm, x)

    """
    def test_rnn(self):
        rnn = nn.RNN(30, 20, 2)
        input = Variable(torch.randn(10, 32, 30))
        output, hidden = rnn(input)
        self.assertONNX(rnn, input)
    """

    def test_symbolic_override_nested(self):
        def symb(g, x, y):
            assert isinstance(x, torch._C.Value)
            assert isinstance(y[0], torch._C.Value)
            assert isinstance(y[1], torch._C.Value)
            return g.op('Sum', x, y[0], y[1]), (
                g.op('Neg', x), g.op('Neg', y[0]))

        @symbolic_override(symb)
        def foo(x, y):
            return x + y[0] + y[1], (-x, -y[0])

        class BigModule(torch.nn.Module):
            def forward(self, x, y):
                return foo(x, y)

        inp = (Variable(torch.FloatTensor([1])),
               (Variable(torch.FloatTensor([2])),
                Variable(torch.FloatTensor([3]))))
        BigModule()(*inp)
        self.assertONNX(BigModule(), inp)



if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--onnx-test', action='store_true', default=False)
    args, remaining = parser.parse_known_args()
    _onnx_test = args.onnx_test
    if _onnx_test:
        for d in glob.glob(os.path.join(test_onnx_common.generated_dir, "test_operator_*")):
            shutil.rmtree(d)
    common.UNITTEST_ARGS = [sys.argv[0]] + remaining
    run_tests()
