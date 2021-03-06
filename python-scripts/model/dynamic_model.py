import torch
import torch.nn as nn
import torch.nn.functional as F
from . import MLP, TCN, LSTM
from .base import RunMode, CausalConvNet


class DynamicModel(nn.Module):
    def __init__(self, model, num_inputs, num_outputs, ar, io_delay, normalizer_input=None, normalizer_output=None,
                 *args, **kwargs):
        super(DynamicModel, self).__init__()
        # Save parameters
        self.num_inputs = num_inputs
        self.num_outputs = num_outputs
        self.args = args
        self.kwargs = kwargs
        self.ar = ar
        self.io_delay = io_delay
        self.normalizer_input = normalizer_input
        self.normalizer_output = normalizer_output
        self.zero_initial_state = False

        # Initialize model
        if model == 'mlp':
            self.m = MLP(self.num_model_inputs, self.num_outputs, *self.args, **self.kwargs)
        elif model == 'tcn':
            self.m = TCN(self.num_model_inputs, self.num_outputs, *self.args, **self.kwargs)
        elif model == 'lstm':
            self.m = LSTM(self.num_model_inputs, self.num_outputs, *self.args, **self.kwargs)
        else:
            raise Exception("Unimplemented model")
        self.mode = RunMode.ONE_STEP_AHEAD
        if isinstance(self.m, CausalConvNet):
            self.m.set_mode('dilation')

    @property
    def num_model_inputs(self):
        return self.num_inputs + self.num_outputs if self.ar else self.num_inputs

    def set_mode(self, mode, zero_initial_state=False):
        self.mode = mode
        self.zero_initial_state = zero_initial_state
        if mode == RunMode.ONE_STEP_AHEAD:
            self.m.set_requested_output('same')
        elif mode == RunMode.FREE_RUN_SIMULATION:
            self.m.set_requested_output(1)
        else:
            raise AttributeError('Unknown mode {}'.format(mode))

    def one_step_ahead(self, u, y):
        num_batches, _, _ = u.size()
        u_delayed = DynamicModel._get_u_delayed(u, self.io_delay)
        if self.ar:
            y_delayed = F.pad(y[:, :, :-1], [1, 0])
            x = torch.cat((u_delayed, y_delayed), 1)
        else:
            x = u_delayed

        if self.m.has_internal_state:
            state_0 = self.m.init_hidden(num_batches, u.device)
            y_pred, state_f = self.m(x, state_0)
        else:
            y_pred = self.m(x)

        return y_pred

    def free_run_simulation(self, u, y):
        if self.ar:
            rf = self.m.get_requested_input(requested_output=1)
            num_batches, _, seq_len = u.size()
            y_sim = y.clone()

            u_delayed = DynamicModel._get_u_delayed(u, self.io_delay)

            if self.m.has_internal_state:
                state = self.m.init_hidden(num_batches, u.device)

            start = 0 if self.zero_initial_state else rf
            for i in range(start, seq_len):
                if i < rf:
                    y_in = F.pad(y_sim[:, :, :i], [rf-i, 0])
                    u_in = F.pad(u_delayed[:, :, :i+1], [rf-i-1, 0])
                else:
                    y_in = y_sim[:, :, i-rf:i]
                    u_in = u_delayed[:, :, i-rf+1:i+1]

                x = torch.cat((u_in, y_in), 1)

                if self.m.has_internal_state:
                    y_next, state = self.m(x, state)
                    y_sim[:, :, i] = y_next[:, :, -1]
                else:
                    y_next = self.m(x)
                    y_sim[:, :, i] = y_next[:, :, -1]
        else:
            y_sim = self.one_step_ahead(u, y)
        return y_sim

    @staticmethod
    def _get_u_delayed(u, io_delay):
        if io_delay > 0:
            u_delayed = F.pad(u[:, :, :-io_delay], [io_delay, 0])
        elif io_delay < 0:
            u_delayed = F.pad(u[:, :, -io_delay:], [0, -io_delay])
        else:
            u_delayed = u
        return u_delayed

    def forward(self, u, y=None):
        if self.normalizer_input is not None:
            u = self.normalizer_input.normalize(u)
        if y is not None and self.normalizer_output is not None:
            y = self.normalizer_output.normalize(y)

        if self.mode == RunMode.ONE_STEP_AHEAD:
            y_pred = self.one_step_ahead(u, y)
        elif self.mode == RunMode.FREE_RUN_SIMULATION:
            y_pred = self.free_run_simulation(u, y)
        else:
            raise Exception("Not implemented mode")

        if self.normalizer_output is not None:
            y_pred = self.normalizer_output.unnormalize(y_pred)
        return y_pred
