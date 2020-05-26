import random
import torch
import torch.nn as nn
import torch.nn.functional as F
from e2e.model.module import BaseRNN
from e2e.model.attention import LocationAwareAttention


class Speller(BaseRNN):
    r"""
    Converts higher level features (from listener) into output utterances
    by specifying a probability distribution over sequences of characters.

    Args:
        num_classes (int): the number of classfication
        max_length (int): a maximum allowed length for the sequence to be processed
        hidden_dim (int): the number of features in the hidden state `h`
        sos_id (int): index of the start of sentence symbol
        eos_id (int): index of the end of sentence symbol
        num_layers (int, optional): number of recurrent layers (default: 1)
        rnn_type (str, optional): type of RNN cell (default: gru)
        dropout_p (float, optional): dropout probability (default: 0)
        device (torch.device): device - 'cuda' or 'cpu'

    Inputs: inputs, listener_outputs, teacher_forcing_ratio
        - **inputs** (batch, seq_len, input_size): list of sequences, whose length is the batch size and within which
          each sequence is a list of token IDs.  It is used for teacher forcing when provided. (default `None`)
        - **listener_outputs** (batch, seq_len, hidden_dim): tensor with containing the outputs of the listener.
          Used for attention mechanism (default is `None`).
        - **teacher_forcing_ratio** (float): The probability that teacher forcing will be used. A random number is
          drawn uniformly from 0-1 for every decoding token, and if the sample is smaller than the given value,
          teacher forcing would be used (default is 0).

    Returns: decoder_outputs
        - **decoder_outputs**: list of tensors containing the outputs of the decoding function.
    """
    def __init__(self, num_classes, max_length, hidden_dim, sos_id, eos_id,
                 num_heads, num_layers=1, rnn_type='gru', dropout_p=0.5, device=None):
        super(Speller, self).__init__(hidden_dim, hidden_dim, num_layers, rnn_type, dropout_p, False, device)
        self.num_classes = num_classes
        self.num_heads = num_heads
        self.max_length = max_length
        self.eos_id = eos_id
        self.sos_id = sos_id
        self.embedding = nn.Embedding(num_classes, hidden_dim)
        self.input_dropout = nn.Dropout(dropout_p)
        self.attention = LocationAwareAttention(hidden_dim, num_heads, conv_out_channel=10)
        self.fc1 = nn.Linear(hidden_dim << 1, hidden_dim, bias=True)
        self.fc2 = nn.Linear(self.hidden_dim, num_classes, bias=True)

    def forward_step(self, input_var, hidden, listener_outputs, align):
        batch_size = input_var.size(0)
        output_lengths = input_var.size(1)

        embedded = self.embedding(input_var).to(self.device)
        embedded = self.input_dropout(embedded)

        if self.training:
            self.rnn.flatten_parameters()

        output, hidden = self.rnn(embedded, hidden)
        context, align = self.attention(output, listener_outputs, align)

        combined = torch.cat([context, output], dim=2)
        output = self.fc1(combined.view(-1, self.hidden_dim << 1)).view(batch_size, -1, self.hidden_dim)
        output = self.fc2(output.contiguous().view(-1, self.hidden_dim))

        step_output = F.log_softmax(output, dim=1)
        step_output = step_output.view(batch_size, output_lengths, -1).squeeze(1)

        return step_output, hidden, align

    def forward(self, inputs, listener_outputs, teacher_forcing_ratio=0.90):
        hidden, align = None, None
        decoder_outputs = list()

        inputs, batch_size, max_length = self.validate_args(inputs, listener_outputs, teacher_forcing_ratio)
        use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False

        if use_teacher_forcing:
            inputs = inputs[inputs != self.eos_id].view(batch_size, -1)

            # Call forward_step() at every timestep because MultiHybridAttention requires previous alignment.
            for di in range(inputs.size(1)):
                input_var = inputs[:, di].unsqueeze(1)
                step_output, hidden, align = self.forward_step(input_var, hidden, listener_outputs, align)
                decoder_outputs.append(step_output)

        else:
            input_var = inputs[:, 0].unsqueeze(1)

            for di in range(max_length):
                step_output, hidden, align = self.forward_step(input_var, hidden, listener_outputs, align)
                decoder_outputs.append(step_output)

                input_var = decoder_outputs[-1].topk(1)[1]

        return decoder_outputs

    def validate_args(self, inputs, listener_outputs, teacher_forcing_ratio):
        """ Validate arguments """
        batch_size = listener_outputs.size(0)

        if inputs is None:  # inference
            inputs = torch.IntTensor([self.sos_id] * batch_size).view(batch_size, 1)
            max_length = self.max_length

            if torch.cuda.is_available():
                inputs = inputs.cuda()

            if teacher_forcing_ratio > 0:
                raise ValueError("Teacher forcing has to be disabled (set 0) when no inputs is provided.")

        else:
            max_length = inputs.size(1) - 1  # minus the start of sequence symbol

        return inputs, batch_size, max_length