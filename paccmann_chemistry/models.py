"""Model Classes Module."""
import torch
import torch.nn as nn
from .stack_rnn import StackGRU
from .utils import get_device
from .hyperparams import ACTIVATION_FACTORY
from itertools import takewhile


class StackGRUEncoder(StackGRU):
    """Stacked GRU Encoder."""

    def __init__(self, params, *args, **kwargs):
        """
        Initializer.

        Args:
        params (dict): it contains size of the latent mean (mu) and variance
            (logvar)
        *args, **kwargs: additional positional and keyword arguments
            inherited from StackGRU.
        """
        super(StackGRUEncoder, self).__init__(params, *args, **kwargs)
        self.params = params
        self.latent_dim = params['latent_dim']
        self.hidden_to_mu = nn.Linear(
            in_features=self.hidden_size * self.n_directions,
            out_features=self.latent_dim
        )
        self.hidden_to_logvar = nn.Linear(
            in_features=self.hidden_size * self.n_directions,
            out_features=self.latent_dim
        )
        self.activation = ACTIVATION_FACTORY[self.activation]

    def encoder_train_step(self, input_seq):
        """
        The Encoder Train Step.

        Args:
            input_seq (torch.Tensor): the sequence of indices for the input
            of shape [max batch sequence length +1, batch_size]. +1 is for
            the added start_index

        Note: input_seq is an output of seq_data_prep(batch) with batches
            returned by a DataLoader object

        Returns:
            mu (torch.Tensor): the latent mean of shape
                [1, batch_size, latent_dim]
            logvar (torch.Tensor): log of the latent variance of shape
                [1, batch_size, latent_dim]
        """
        hidden = self.init_hidden()
        stack = self.init_stack()
        for c in range(len(input_seq)):
            output, hidden, stack = self(input_seq[c], hidden, stack)

        # Reshape to disentangle layers and directions
        hidden = hidden.view(
            self.n_layers, self.n_directions, self.batch_size, self.hidden_size
        )
        # Discard all but last layer, concatenate forward/backward
        hidden = hidden[-1, :, :, :].view(
            self.batch_size, self.hidden_size * self.n_directions
        )
        mu = self.hidden_to_mu(hidden)
        logvar = self.hidden_to_logvar(hidden)

        return mu, logvar


class StackGRUDecoder(StackGRU):
    """Stack GRU Decoder."""

    def __init__(self, params, *args, **kwargs):
        """
        Initializer.

        Args:
            params (dict): it contains size of the latent mean (mu) and variance
                (logvar)
            *args, **kwargs: additional positional and keyword arguments
                inherited from StackGRU.
        """
        super(StackGRUDecoder, self).__init__(params, *args, **kwargs)
        self.params = params
        self.latent_dim = params['latent_dim']
        self.latent_to_hidden = nn.Linear(
            in_features=self.latent_dim, out_features=self.hidden_size
        )
        self.activation = ACTIVATION_FACTORY[self.activation]

    def decoder_train_step(self, latent_z, input_seq, target_seq):
        """
        The Decoder Train Step.

        Args:
            latent_z (torch.Tensor): the sampled latent representation
                of the SMILES to be used for generation of shape
                [1, batch_size, latent_dim]
            input_seq (torch.Tensor): the sequence of indices for the
                input of size [max batch sequence length +1, batch_size]. +1
                is for the added start_index
            target_seq (torch.Tensor): the sequence of indices for the
                target of shape [max batch sequence length +1, batch_size]. +1
                is for the added end_index

        Note: input_seq and target_seq are outputs of
            generator.data.seq_data_prep(batch) with batches
            returned by a DataLoader object

        Returns: the cross-entropy training loss for the decoder.
        """
        hidden = self.latent_to_hidden(latent_z)
        stack = self.init_stack()
        loss = 0
        for idx in range(len(input_seq)):
            output, hidden, stack = self(input_seq[idx], hidden, stack)
            loss += self.criterion(output, target_seq[idx].squeeze())
        return loss

    def generate_from_latent(
        self,
        latent_z,
        prime_input,
        end_token,
        generate_len=100,
        temperature=0.8
    ):
        """
        Generate SMILES From Latent Z.

        Args:
            latent_z (torch.Tensor): the sampled latent representation
                of size [1, batch_size, latent_dim]
            prime_input (torch.Tensor): tensor of indices for the priming
                string. Must be of size: [1, prime_input length] or
                [prime_input length]

            Example:

                prime_input = [2, 4, 5]
                prime_input = torch.tensor(prime_input).view(1, -1)

                or

                prime_input = [2, 4, 5]
                prime_input = torch.tensor(prime_input)

            end_token (torch.Tensor): End token for the generated molecule
                of shape [1].

            Example:

                end_token = torch.LongTensor([3])

            generate_len (int): Length of the generated molecule
            temperature (float): softmax temperature parameter between
                0 and 1. Lower temperatures result in a more descriminative
                softmax.

        Returns:
            generated_seq (torch.Tensor): the tensor of sequence(s) for the
                generated molecule(s) of shape
                [batch_size, generate_len + len(prime_input)]

        Note: for each generated sequence all indices after the first
            end_token must be discarded
        """
        n_layers = self.n_layers
        n_directions = self.n_directions
        latent_z = latent_z.repeat(n_layers * n_directions, 1, 1)
        hidden = self.latent_to_hidden(latent_z)
        batch_size = hidden.shape[1]
        stack = self.init_stack(batch_size)
        prime_input = prime_input.repeat(batch_size, 1)
        prime_input = prime_input.transpose(1, 0).view(-1, 1, len(prime_input))
        generated_seq = prime_input.transpose(0, 2)

        # Use priming string to "build up" hidden state
        for p in range(len(prime_input) - 1):
            _, hidden, stack = self.forward(prime_input[p], hidden, stack)
        input_token = prime_input[-1].to(get_device())

        for p in range(generate_len):
            output, hidden, stack = self.forward(input_token, hidden, stack)

            # Sample from the network as a multinomial distribution
            output_dist = output.data.cpu().view(batch_size, 1).div(
                temperature
            ).exp().double()    # yapf: disable
            top_idx = torch.tensor(
                torch.multinomial(output_dist, 1).cpu().numpy()
            )
            # Add generated_seq character to string and use as next input
            generated_seq = torch.cat(
                (generated_seq, top_idx.unsqueeze(2)),
                dim=2
            )   # yapf: disable
            input_token = top_idx.view(1, -1)
            # break when end token is generated
            if batch_size == 1 and top_idx == end_token:
                break
        return generated_seq


class TeacherVAE(nn.Module):

    def __init__(self, encoder, decoder):
        """
        Initialization.

        Args:
            encoder (StackGRUEncoder): the encoder object.
            decoder (StackGRUDecoder): the decoder object.
        """
        super(TeacherVAE, self).__init__()
        self.encoder = encoder
        self.decoder = decoder

    def encode(self, input_seq):
        """
        VAE Encoder.
        Args:
            input_seq (torch.Tensor): the sequence of indices for the input
                of shape [max batch sequence length +1, batch_size]. +1 is for
                the added start_index

        Returns:
            mu (torch.Tensor): the latent mean of shape
                [1, batch_size, latent_dim]
            logvar (torch.Tensor): log of the latent variance of shape
                [1, batch_size, latent_dim]
        """
        mu, logvar = self.encoder.encoder_train_step(input_seq)
        return mu, logvar

    def reparameterize(self, mu, logvar):
        """
        Sample Z From Latent Dist.

        Args:
            mu (torch.Tensor): the latent mean of shape
                [1, batch_size, latent_dim]
            logvar (torch.Tensor): log of the latent variance of shape
                [1, batch_size, latent_dim]

        Returns:
            Sampled latent z from the latent distribution of shape
            [1, batch_size, latent_dim]
        """
        std = torch.exp(0.5 * logvar)
        eps = torch.randn_like(std)
        return eps.mul(std).add_(mu)

    def decode(self, latent_z, input_seq, target_seq):
        """
        Decode The Latent Z (for training).

        Args:
            latent_z (torch.Tensor): the sampled latent representation
                of the SMILES to be used for generation of shape
                [1, batch_size, latent_dim]
            input_seq (torch.Tensor): the sequence of indices for the input
                of shape [max batch sequence length +1, batch_size]. +1 is for
                the added start_index
            target_seq (torch.Tensor): the sequence of indices for the
                target of shape [max batch sequence length +1, batch_size]. +1
                is for the added end_index

        Note: input_seq and target_seq are outputs of
            generator.data.seq_data_prep(batch) with batches
            returned by a DataLoader object

        Returns: the cross-entropy training loss for the decoder.
        """
        n_layers = self.decoder.n_layers
        n_directions = self.decoder.n_directions
        latent_z = latent_z.repeat(n_layers * n_directions, 1, 1)
        decoder_loss = self.decoder.decoder_train_step(
            latent_z, input_seq, target_seq
        )
        return decoder_loss

    def forward(self, input_seq, decoder_seq, target_seq):
        """
        The Forward Function.

        Args:
            input_seq (torch.Tensor): the sequence of indices for the input
                of shape [max batch sequence length +1, batch_size]. +1 is for
                the added start_index.
            target_seq (torch.Tensor): the sequence of indices for the
                target of shape [max batch sequence length +1, batch_size]. +1
                is for the added end_index.

        Note: input_seq and target_seq are outputs of
            generator.data.seq_data_prep(batch) with batches
            returned by a DataLoader object.

        Returns:
            decoder_loss: the cross-entropy training loss for the decoder.
            mu (torch.Tensor): the latent mean of shape
                [1, batch_size, latent_dim].
            logvar (torch.Tensor): log of the latent variance of shape
                [1, batch_size, latent_dim].
        """
        mu, logvar = self.encode(input_seq)
        latent_z = self.reparameterize(mu, logvar)
        decoder_loss = self.decode(latent_z, decoder_seq, target_seq)
        return decoder_loss, mu, logvar

    def generate(
        self,
        latent_z,
        prime_input,
        end_token,
        generate_len=100,
        temperature=0.8
    ):
        """
        Generate SMILES From Latent Z.

        Args:
            latent_z (torch.Tensor): the sampled latent representation
                of size [1, batch_size, latent_dim].
            prime_input (torch.Tensor): tensor of indices for the priming
                string. Must be of size: [1, prime_input length] or
                [prime_input length].

                Example:

                    prime_input = [2, 4, 5]
                    prime_input = torch.tensor(prime_input).view(1, -1)

                    or

                    prime_input = [2, 4, 5]
                    prime_input = torch.tensor(prime_input)

            end_token (torch.Tensor): End token for the generated molecule
                of shape [1].

                Example:

                    end_token = torch.LongTensor([3])

            generate_len (int): Length of the generated molecule
                temperature (float): softmax temperature parameter between
                0 and 1. Lower temperatures result in a more descriminative
                softmax.

        Returns:
            molecule_iter (map): an iterator returning the torch tensor of
                sequence(s) for the generated molecule(s) of shape
                [sequence length].

        Note: the start and end tokens are automatically stripped
            from the returned torch tensors for the generated molecule.
        """
        generated_batch = self.decoder.generate_from_latent(
            latent_z,
            prime_input,
            end_token,
            generate_len=generate_len,
            temperature=temperature
        )

        molecule_gen = (
            takewhile(lambda x: x != end_token, molecule.squeeze()[1:])
            for molecule in generated_batch
        )   # yapf: disable

        molecule_map = map(list, molecule_gen)
        molecule_iter = iter(map(torch.tensor, molecule_map))

        return molecule_iter

    def save_model(self, path, *args, **kwargs):
        """Save Model to Path."""
        torch.save(self.state_dict(), path, *args, **kwargs)

    def load_model(self, path, *args, **kwargs):
        """Load Model From Path."""
        weights = torch.load(path, *args, **kwargs)
        self.load_state_dict(weights)
