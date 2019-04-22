#!/usr/bin/env python
__author__ = 'arenduchintala'
import math
import torch
import torch.nn as nn

from src.models.model_untils import BiRNNConextEncoder
from src.models.model_untils import SelfAttentionalContextEncoder

from torch.nn.utils.rnn import pack_padded_sequence as pack
from torch.nn.utils.rnn import pad_packed_sequence as unpack
from src.utils.utils import SPECIAL_TOKENS

from src.rewards import score_embeddings
from src.rewards import rank_score_embeddings
from src.rewards import get_nearest_neighbors

from src.opt.noam import NoamOpt

import pickle
import pdb


def make_l2_tied_encoder_decoder(l1_tied_enc_dec,
                                 v2i, c2i, vspelling,
                                 g2i, gc2i, gv2spelling):
    if isinstance(l1_tied_enc_dec, CharTiedEncoderDecoder):
        assert gv2spelling is not None
        v2spell = pickle.load(open(gv2spelling, 'rb'))
        spelling_mat = torch.Tensor(len(v2spell), len(v2spell[0])).fill_(0).long()
        for k, v in v2spell.items():
            spelling_mat[k] = torch.tensor(v)
        spelling_mat = spelling_mat[:, :-1] # throw away length of spelling because we going to use cnns
        l2_tied_encoder_decoder = CharTiedEncoderDecoder(char_vocab_size=len(gc2i),
                                                         char_embedding_size=l1_tied_enc_dec.char_embedding.embedding_dim,
                                                         word_vocab_size=len(g2i),
                                                         word_embedding_size=l1_tied_enc_dec.word_embedding_size,
                                                         spelling_mat=spelling_mat,
                                                         mode='l2')
        #TODO: match the char_embs
        #TODO: copy params of the cnn
        pdb.set_trace()
        return l2_tied_encoder_decoder
    elif isinstance(l1_tied_enc_dec, TiedEncoderDecoder):
        l2_tied_encoder_decoder = TiedEncoderDecoder(vocab_size=len(g2i),
                                                     embedding_size=l1_tied_enc_dec.embedding.embedding_dim,
                                                     mode='l2',
                                                     vmat=None)
        l2_tied_encoder_decoder.embedding.weight.data.uniform_(-0.01, 0.01)
        #pdb.set_trace()
        return l2_tied_encoder_decoder
    else:
        raise NotImplementedError("unknown tied_encoder_decoder")


class TiedEncoderDecoder(nn.Module):
    def __init__(self, vocab_size, embedding_size, mode, vmat=None):
        super().__init__()
        self.mode = mode
        self.mode in ['l1', 'l2']
        self.embedding = torch.nn.Embedding(vocab_size, embedding_size)
        self.embedding.weight.data.normal_(0, 1.0)
        if vmat is not None:
            self.embedding.weight.data = vmat
            self.pretrained = True
        else:
            self.pretrained = False
        self.decoder = torch.nn.Linear(embedding_size, vocab_size, bias=False)
        self.decoder.weight = self.embedding.weight

    def init_param_freeze(self, weight_type):
        if self.mode == 'l1':
            if weight_type == 'l1':
                self.embedding.weight.requres_grad = True and not self.pretrained
            else:
                raise NotImplementedError("weight type should only be l1 in mode=l1")
        else:
            assert not self.pretrained
            if weight_type == 'l1':
                self.embedding.weight.requires_grad = False
            elif weight_type == 'l2':
                self.embedding.weight.requires_grad = True
            else:
                raise NotImplementedError("unknown weight_type/mode combination")
        return True

    def embedding_dim(self,):
        return self.embedding.embedding_dim

    def forward(self, data, mode):
        if mode == 'input':
            return self.embedding(data)
        elif mode == 'output':
            return self.decoder(data)
        else:
            raise NotImplementedError("unknown mode")

    def init_cuda(self,):
        self = self.cuda()
        return True

    def is_cuda(self,):
        return self.embedding.weight.data.is_cuda

    def get_weight(self, on_device=True):
        if on_device:
            weights = self.embedding.weight.data.clone().detach()
        else:
            weights = self.embedding.weight.data.clone().detach().cpu()
        return weights

    def get_params(self,):
        return self.get_weight()

    def set_params(self, new_params):
        self.embedding.weight.data = new_params.clone()
        self.decoder.weight = self.embedding.weight
        return True

    def get_weight_without_detach(self,):
        return self.embedding.weight

    def regularized_step(self, new_params):
        #self.l2_encoder.weight.data = 0.3 * l2_encoder_cpy + 0.7 * self.l2_encoder.weight.data
        self.set_params(0.3 * new_params + 0.7 * self.get_params())
        return True


class CharTiedEncoderDecoder(nn.Module):
    def __init__(self, char_vocab_size, char_embedding_size,
                 word_vocab_size, word_embedding_size, spelling_mat, mode):
        super().__init__()
        self.mode = mode
        assert self.mode in ['l1', 'l2']
        self.word_vocab_size = word_vocab_size
        self.word_embedding_size = word_embedding_size
        self.char_embedding_size = char_embedding_size
        self.num_chars = spelling_mat.shape[1]
        self.spelling_embedding = torch.nn.Embedding(word_vocab_size, self.num_chars)
        self.spelling_embedding.weight.data = spelling_mat
        self.spelling_embedding.weight.requires_grad = False
        self.char_embedding = torch.nn.Embedding(char_vocab_size, char_embedding_size)
        self.char_embedding.weight.data.normal_(0, 1.0)
        ks = 4
        self.cnn = nn.Conv1d(self.char_embedding_size + 1, self.word_embedding_size,
                             kernel_size=ks, padding=0, bias=False)
        self.mp = nn.MaxPool1d(self.num_chars - (ks - 1))
        # approximating many smaller kernel sizes with one large kernel by zeroing out kernel elements
        _k = self.word_embedding_size // ks
        for i in range(1, ks):
            x = torch.Tensor(_k, self.char_embedding_size + 1, ks).uniform_(-0.05, 0.05)
            x[:, :, torch.arange(i, ks).long()] = 0
            self.cnn.weight.data[torch.arange((i - 1) * _k, i * _k).long(), :, :] = x

    def embedding_dim(self,):
        return self.word_embedding_size

    def init_param_freeze(self, weight_type):
        if self.mode == 'l1':
            if weight_type == 'l1':
                self.char_embedding.weight.requires_grad = True
                self.cnn.weight.requires_grad = True
            else:
                raise NotImplementedError("weight type should only be l1 in mode=l1")
        else:
            if weight_type == 'l1':
                self.char_embedding.weight.requires_grad = False
                self.cnn.weight.requires_grad = False
            elif weight_type == 'l2':
                self.char_embedding.weight.requires_grad = True
                self.cnn.weight.requires_grad = True
            else:
                raise NotImplementedError("unknown weight_type/mode combination")
        self.spelling_embedding.weight.requires_grad = False
        return True

    def input_forward(self, data):
        #data shape = (bsz, seqlen)
        spelling = self.spelling_embedding(data).long()
        # spelling shape = (bsz, seqlen, num_chars)
        char_emb = self.char_embedding(spelling)
        bsz, seq_len, num_chars, char_emb_size = char_emb.shape
        # char_emb shape = (bsz, seqlen, num_chars, char_emb_size)
        char_emb = char_emb.view(-1, num_chars, char_emb_size)
        # char_emb shape = (bsz * seqlen, num_chars, char_emb_sze)
        lang_bit = torch.ones(1, 1, 1).type_as(char_emb).expand(char_emb.shape[0], char_emb.shape[1], 1)
        lang_bit = lang_bit * 1 if self.mode == 'l2' else lang_bit * 0
        char_emb = torch.cat([char_emb, lang_bit], dim=2)
        char_emb = char_emb.transpose(1, 2)
        # char_emb shape = (bsz * seq_len, char_emb_size + 1, num_chars)
        word_emb = self.mp(self.cnn(char_emb)).squeeze(2)
        #word_emb shape = (bsz * seq_len, word_emb_size)
        word_emb = word_emb.view(bsz, seq_len, -1)
        return word_emb

    def _compute_word_embeddings(self,):
        spelling = self.spelling_embedding.weight.data.clone().detach().long()
        # spelling shape = (v, num_chars)
        char_emb = self.char_embedding(spelling)
        #v, num_chars, char_emb_size = char_emb.shape
        lang_bit = torch.ones(1, 1, 1).type_as(char_emb).expand(char_emb.shape[0], char_emb.shape[1], 1)
        lang_bit = lang_bit * 1 if self.mode == 'l2' else lang_bit * 0
        char_emb = torch.cat([char_emb, lang_bit], dim=2)
        char_emb = char_emb.transpose(1, 2)
        word_emb = self.mp(self.cnn(char_emb)).squeeze(2)
        return word_emb

    def output_forward(self, data):
        v = torch.arange(self.word_vocab_size).type_as(data).long()
        spelling = self.spelling_embedding(v).long()
        # spelling shape = (v, num_chars -1)
        char_emb = self.char_embedding(spelling)
        #v, num_chars, char_emb_size = char_emb.shape
        lang_bit = torch.ones(1, 1, 1).type_as(char_emb).expand(char_emb.shape[0], char_emb.shape[1], 1)
        lang_bit = lang_bit * 1 if self.mode == 'l2' else lang_bit * 0
        char_emb = torch.cat([char_emb, lang_bit], dim=2)
        char_emb = char_emb.transpose(1, 2)
        word_emb = self.mp(self.cnn(char_emb)).squeeze(2)
        #word_emb shape = (v, word_emb_size)
        #return self.__decoder(data)
        return torch.matmul(data, word_emb.transpose(0, 1))

    def forward(self, data, mode):
        if mode == 'input':
            return self.input_forward(data)
        elif mode == 'output':
            return self.output_forward(data)
        else:
            raise NotImplementedError("unknown mode")

    def init_cuda(self,):
        self = self.cuda()
        return True

    def is_cuda(self,):
        return self.char_embedding.weight.data.is_cuda

    def get_weight(self, on_device=True):
        weights = self._compute_word_embeddings().detach().clone()
        if on_device:
            pass
        else:
            weights = weights.cpu()
        return weights

    def get_params(self,):
        return self.named_parameters()

    def set_params(self, new_params):
        for n, p in self.named_parameters():
            for _n, _p in new_params:
                if _n == n:
                    p.data = _p.data
        return True

    def regularized_step(self, new_params):
        self.set_params(0.9 * new_params + 0.1 * self.get_params())
        return True



class ContextEncoder(nn.Module):
    def __init__(self,):
        super().__init__()

    def forward(self, encoded, lengths, forward_mode):
        pass

    def init_cuda(self):
        pass

    def is_cuda(self,):
        pass


class LMContextEncoder(ContextEncoder):
    def __init__(self, input_size, rnn_size, num_layers):
        super().__init__()
        self.input_size = input_size
        self.rnn_size = rnn_size
        self.num_layers = num_layers
        self.lstm = nn.LSTM(self.input_size, self.rnn_size,
                            num_layers=num_layers,
                            batch_first=True,
                            bidirectional=False)
        self.hidden_size = self.rnn_size
        self.projection = torch.nn.Linear(self.hidden_size, self.input_size)
        self.dropout = torch.nn.Dropout(0.1)

    def init_cuda(self,):
        self = self.cuda()
        return True

    def is_cuda(self,):
        for p in self.lstm.parameters():
            return p.is_cuda
        return False

    def forward(self, encoded, lengths, forward_mode):
        assert forward_mode in ['L1', 'L2']
        if forward_mode == 'L1':
            encoded = self.dropout(encoded)
        else:
            pass
        packed_encoded = pack(encoded, lengths, batch_first=True)
        packed_hidden, (h_t, c_t) = self.lstm(packed_encoded)
        hidden, lengths = unpack(packed_hidden, batch_first=True)
        z = torch.ones(hidden.size(0), 1, hidden.size(2)).type_as(hidden)
        z.requires_grad = False
        hidden = hidden[:, :-1, :]
        hidden = torch.cat([z, hidden], dim=1)
        if forward_mode == 'L1':
            hidden = self.dropout(hidden)
        else:
            pass
        projected_hidden = self.projection(hidden)
        return projected_hidden


class ClozeContextEncoder(ContextEncoder):
    def __init__(self, input_size, rnn_size, num_layers):
        super().__init__()
        self.input_size = input_size
        self.rnn_size = rnn_size
        self.num_layers = num_layers
        self.lstm = nn.LSTM(self.input_size, self.rnn_size,
                            num_layers=num_layers,
                            batch_first=True,
                            bidirectional=True)
        self.z = torch.zeros(1, 1, self.rnn_size, requires_grad=False)
        self.hidden_size = 2 * self.rnn_size
        self.projection = torch.nn.Linear(self.hidden_size, self.input_size)
        self.dropout = torch.nn.Dropout(0.1)

    def init_cuda(self,):
        self = self.cuda()
        self.z = self.z.cuda()
        return True

    def is_cuda(self,):
        for p in self.lstm.parameters():
            return p.is_cuda
        return False

    def forward(self, encoded, lengths, forward_mode):
        assert forward_mode in ['L1', 'L2']
        batch_size, seq_len, emb_size = encoded.shape
        if forward_mode == 'L1':
            encoded = self.dropout(encoded)
        else:
            pass
        packed_encoded = pack(encoded, lengths, batch_first=True)
        # encoded = (batch_size x seq_len x embedding_size)
        packed_hidden, (h_t, c_t) = self.lstm(packed_encoded)
        hidden, lengths = unpack(packed_hidden, batch_first=True)
        z = self.z.expand(batch_size, 1, self.rnn_size)
        fwd_hidden = torch.cat((z, hidden[:, :-1, :self.rnn_size]), dim=1)
        bwd_hidden = torch.cat((hidden[:, 1:, self.rnn_size:], z), dim=1)
        # bwd_hidden = (batch_size x seq_len x rnn_size)
        # fwd_hidden = (batch_size x seq_len x rnn_size)
        hidden = torch.cat((fwd_hidden, bwd_hidden), dim=2)
        if forward_mode == 'L1':
            hidden = self.dropout(hidden)
        else:
            pass
        projected_hidden = self.projection(hidden)
        return projected_hidden


class ClozeMaskContextEncoder(ContextEncoder):
    def __init__(self, input_size, rnn_size, num_layers):
        super().__init__()
        self.input_size = input_size
        self.rnn_size = rnn_size
        self.num_layers = num_layers
        self.lstm = nn.LSTM(self.input_size, self.rnn_size,
                            num_layers=num_layers,
                            batch_first=True,
                            bidirectional=True)
        self.hidden_size = 2 * self.rnn_size
        self.projection = torch.nn.Linear(self.hidden_size, self.input_size)
        self.mask_prob = 0.3
        noise_probs = torch.tensor([1.0 - self.mask_prob, self.mask_prob])
        self.noise_mask = torch.distributions.Categorical(probs=noise_probs)

    def init_cuda(self,):
        self = self.cuda()
        noise_probs = torch.tensor([1.0 - self.mask_prob, self.mask_prob]).cuda()
        self.noise_mask = torch.distributions.Categorical(probs=noise_probs)
        return True

    def is_cuda(self,):
        for p in self.lstm.parameters():
            return p.is_cuda
        return False

    def word_mask(self, encoded):
        n_idx = self.noise_mask.sample(sample_shape=(encoded.size(0), encoded.size(1)))
        encoded[n_idx == 1] = 0.
        return encoded

    def forward(self, encoded, lengths, forward_mode):
        assert forward_mode in ['L1', 'L2']
        if forward_mode == 'L1':
            encoded = self.word_mask(encoded)
        else:
            pass
        packed_encoded = pack(encoded, lengths, batch_first=True)
        packed_hidden, (h_t, c_t) = self.lstm(packed_encoded)
        hidden, lengths = unpack(packed_hidden, batch_first=True)
        if forward_mode == 'L1':
            projected_hidden = self.projection(torch.nn.functional.dropout(hidden, p=0.1, training=self.training))
        else:
            projected_hidden = self.projection(hidden)
        return projected_hidden


class CE_CLOZE(nn.Module):
    def __init__(self,
                 tied_encoder_decoder,
                 context_encoder,
                 l1_dict,
                 loss_at='all',
                 dropout=0.1,
                 max_grad_norm=5.):
        super().__init__()
        self.tied_encoder_decoder = tied_encoder_decoder
        self.context_encoder = context_encoder
        self.loss_at = loss_at
        self.l1_dict = l1_dict
        self.loss = torch.nn.CrossEntropyLoss(reduction='sum', ignore_index=0)
        self.max_grad_norm = max_grad_norm
        #self.init_cuda()
        self.init_param_freeze()
        self.init_optimizer('Adam')

    def init_cuda(self,):
        self.context_encoder.init_cuda()
        self.tied_encoder_decoder.init_cuda()
        self = self.cuda()
        return True

    def init_param_freeze(self,):
        for n, p in self.context_encoder.named_parameters():
            p.requires_grad = True
        self.tied_encoder_decoder.init_param_freeze(weight_type='l1')
        for n, p in self.named_parameters():
            print(n, p.requires_grad)
        return True

    def is_cuda(self,):
        return self.context_encoder.is_cuda()

    def init_optimizer(self, type='Adam', lr=1.0):
        if type == 'Adam':
            self.optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad, self.parameters()))
        elif type == 'SGD':
            self.optimizer = torch.optim.SGD(filter(lambda p: p.requires_grad, self.parameters()), lr=lr)
        elif type == 'noam':
            _optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad, self.parameters()))
            self.optimizer = NoamOpt(self.emb_size, 100, _optimizer)
        else:
            raise NotImplementedError("unknown optimizer option")

    #def get_noise_channel(self, l1_data, l1_encoded):
    #    n_idx = self.noise_mask.sample(sample_shape=(l1_data.size(0), l1_data.size(1)))
    #    n_idx[l1_data.eq(self.l1_dict[SPECIAL_TOKENS.PAD])] = 0
    #    n_idx[l1_data.eq(self.l1_dict[SPECIAL_TOKENS.EOS])] = 0
    #    n_idx[l1_data.eq(self.l1_dict[SPECIAL_TOKENS.BOS])] = 0
    #    nns = self.nn_mapper(l1_data)
    #    nns_idx = torch.empty_like(l1_data).random_(0, self.nn_mapper.embedding_dim - 1)
    #    nns_idx = nns_idx.unsqueeze(2).expand_as(nns)
    #    l1_nn = torch.gather(nns, 2, nns_idx)[:, :, 0]
    #    l1_nn_encoded = self.encoder(l1_nn)
    #    if self.noise_profile == 1:
    #        l1_noisy = torch.zeros_like(l1_encoded).type_as(l1_encoded)
    #        l1_noisy[n_idx == 0] = l1_encoded[n_idx == 0]                   # n_idx == 0 no noise
    #        l1_noisy[n_idx == 1] = 0.0                                      # n_idx == 1 blank vector
    #        l1_noisy[n_idx == 2] = l1_nn_encoded[n_idx == 2]                # n_idx == 2 nearest neighbors
    #    elif self.noise_profile == 2:
    #        l1_noisy = torch.zeros_like(l1_encoded).type_as(l1_encoded)
    #        l1_noisy[n_idx == 0] = l1_encoded[n_idx == 0]                   # n_idx == 0 no noise
    #        l1_noisy[n_idx == 1] = 0.0                                      # n_idx == 1 blank vector
    #        l1_noisy[n_idx == 2] = 0.0                                      # n_idx == 2 nearest neighbors
    #    elif self.noise_profile == 3:
    #        l1_rand = torch.empty_like(l1_data).random_(0, self.encoder.num_embeddings)
    #        l1_rand_encoded = self.encoder(l1_rand)
    #        l1_noisy = torch.zeros_like(l1_encoded).type_as(l1_encoded)
    #        l1_noisy[n_idx == 0] = l1_encoded[n_idx == 0]                   # n_idx == 0 no noise
    #        l1_noisy[n_idx == 1] = l1_nn_encoded[n_idx == 1]                # n_idx == 1 nearest neighbors
    #        l1_noisy[n_idx == 2] = l1_rand_encoded[n_idx == 2]              # n_idx == 2 rand words
    #    else:
    #        raise BaseException("unknown noise profile")

    #    n_idx[n_idx.ne(0)] = 1
    #    return l1_noisy, n_idx

    def get_acc(self, arg_top, l1_data):
        acc = float((arg_top == l1_data).nonzero().numel()) / float(l1_data.numel())
        assert 0.0 <= acc <= 1.0
        return acc

    def get_loss(self, pred, target):
        loss = self.loss(pred, target)
        return loss

    def forward(self, batch, get_acc=True):
        lengths, l1_data, _, ind = batch
        l1_idxs = ind.eq(1).long()
        l2_idxs = ind.eq(2).long()
        for st in [SPECIAL_TOKENS.PAD, SPECIAL_TOKENS.UNK]:  # SPECIAL_TOKENS.EOS, SPECIAL_TOKENS.BOS]:
            if st in self.l1_dict:
                l1_idxs[l1_data.eq(self.l1_dict[st])] = 0
                l2_idxs[l1_data.eq(self.l1_dict[st])] = 0
                ind[l1_data.eq(self.l1_dict[st])] = 0
        l1_encoded = self.tied_encoder_decoder(l1_data, mode='input')
        hidden = self.context_encoder(l1_encoded, lengths, forward_mode='L1')
        out = self.tied_encoder_decoder(hidden, mode='output')
        if self.loss_at == 'all':
            out_l = out.view(-1, out.size(-1))
            l1_data_l = l1_data.view(-1)
        else:
            raise BaseException("unknown loss at")
        loss = self.get_loss(out_l, l1_data_l)
        if get_acc:
            _, argmax = out_l.max(dim=1)
            acc = self.get_acc(argmax, l1_data_l)
        else:
            acc = 0.0
        return loss, acc

    def train_step(self, batch):
        _l, _a = self(batch, True)
        _l.backward()
        grad_norm = torch.nn.utils.clip_grad_norm_(filter(lambda p: p.requires_grad, self.parameters()),
                                                   self.max_grad_norm)
        if math.isnan(grad_norm):
            print('skipping update grad_norm is nan!')
        else:
            self.optimizer.step()
        loss = _l.item()
        return loss, grad_norm, _a

    def save_model(self, path):
        torch.save(self, path)

########################################################################################################################
########################################################################################################################


class L2_CE_CLOZE(nn.Module):
    def __init__(self,
                 context_encoder,
                 l1_tied_encoder_decoder,
                 l2_tied_encoder_decoder,
                 l1_dict,
                 l2_dict,
                 l1_key,
                 l2_key,
                 l2_key_wt,
                 learn_step_regularization,
                 learning_steps=3,
                 step_size=0.1):
        super().__init__()
        self.context_encoder = context_encoder
        self.l1_tied_encoder_decoder = l1_tied_encoder_decoder
        self.l2_tied_encoder_decoder = l2_tied_encoder_decoder
        assert self.l2_tied_encoder_decoder.embedding_dim() == self.l1_tied_encoder_decoder.embedding_dim()
        self.l1_dict = l1_dict
        self.l1_dict_idx = {v: k for k, v in l1_dict.items()}
        self.l2_dict = l2_dict
        self.l2_dict_idx = {v: k for k, v in l2_dict.items()}
        self.l1_key = l1_key
        self.l2_key = l2_key
        self.l2_key_wt = l2_key_wt
        self.init_key()
        self.loss = torch.nn.CrossEntropyLoss(reduction='sum', ignore_index=self.l1_dict[SPECIAL_TOKENS.PAD])
        self.l2_exposure = {}
        self.init_param_freeze()
        self.learning_steps = learning_steps
        self.learn_step_regularization = learn_step_regularization
        assert self.learn_step_regularization >= 0.0
        self.optimizer = torch.optim.SGD(filter(lambda p: p.requires_grad, self.parameters()), lr=0.1)
        #self.optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad, self.parameters()))

    def init_key(self,):
        if self.l1_key is not None:
            if self.is_cuda():
                self.l1_key = self.l1_key.cuda()
            else:
                pass
        if self.l2_key is not None:
            if self.is_cuda():
                self.l2_key = self.l2_key.cuda()
            else:
                pass
        if self.l2_key_wt is not None:
            if self.is_cuda():
                self.l2_key_wt = self.l2_key_wt.cuda()
            else:
                pass

    def mix_inputs(self, l1_channel, l2_channel, l1_idxs, l2_idxs):
        assert l1_channel.dim() == l2_channel.dim()
        if len(l1_channel.shape) - len(l1_idxs.shape) == 0:
            v_inp_ind = l1_idxs.float() #.unsqueeze(2).expand_as(l1_channel).float()
            g_inp_ind = l2_idxs.float() #.unsqueeze(2).expand_as(l2_channel).float()
        elif len(l1_channel.shape) - len(l1_idxs.shape) == 1:
            v_inp_ind = l1_idxs.unsqueeze(2).expand_as(l1_channel).float()
            g_inp_ind = l2_idxs.unsqueeze(2).expand_as(l2_channel).float()
            pass
        else:
            raise BaseException("channel and idx mismatch by more than 1 dim!")
        encoded = (v_inp_ind * l1_channel.float() + g_inp_ind * l2_channel.float()).type_as(l1_channel)
        return encoded

    def update_l2_encoder(self, out, l2_data, l2_idxs):
        raise NotImplementedError("todo...")
        return True

    #def inspect_weights(self, l2):
    #    arg_top_seen, cs_top_seen = get_nearest_neighbors(self.l2_encoder.weight.data,
    #                                                      self.l1_encoded.weight.data,
    #                                                      l2, 5)
    #    pass

    def forward(self, batch):
        lengths, l1_data, l2_data, ind, _ = batch
        l1_idxs = ind.eq(1).long()
        l2_idxs = ind.eq(2).long()
        for st in [SPECIAL_TOKENS.PAD, SPECIAL_TOKENS.UNK]:  # SPECIAL_TOKENS.EOS, SPECIAL_TOKENS.BOS]:
            if st in self.l1_dict:
                l1_idxs[l1_data.eq(self.l1_dict[st])] = 0
                l2_idxs[l1_data.eq(self.l1_dict[st])] = 0
                ind[l1_data.eq(self.l1_dict[st])] = 0
        batch_size = l1_data.size(0)
        assert batch_size == 1
        l1_encoded = self.l1_tied_encoder_decoder(l1_data, mode='input')
        l2_encoded = self.l2_tied_encoder_decoder(l2_data, mode='input')
        mixed_encoded = self.mix_inputs(l1_encoded, l2_encoded, l1_idxs, l2_idxs)
        j_data = l1_data.clone()
        j_data[l2_idxs == 1] = l2_data[l2_idxs == 1] + self.l1_tied_encoder_decoder.embedding.num_embeddings
        hidden = self.context_encoder(mixed_encoded, lengths, forward_mode='L2')
        out_l1 = self.l1_tied_encoder_decoder(hidden, mode='output')
        out_l2 = self.l2_tied_encoder_decoder(hidden, mode='output')
        l2_mask = torch.ones(out_l2.size(2)).type_as(l2_data)
        l2_mask[l2_data[0, l2_idxs[0, :] == 1]] = 0
        out_l2[:, :, l2_mask == 1] = float('-inf')
        out = torch.cat([out_l1, out_l2], dim=2)
        loss = self.loss(out.view(-1, out.size(2)), j_data.view(-1))
        return loss

    def regularized_step(self, l2_cpy):
        self.l2_tied_encoder_decoder.regularized_step(l2_cpy)
        return True

    def learn_step(self, batch):
        l2_cpy = self.l2_tied_encoder_decoder.get_params()
        for _ in range(self.learning_steps):
            self.optimizer.zero_grad()
            loss = self(batch)
            l2_regularized = ((l2_cpy - self.l2_tied_encoder_decoder.get_weight_without_detach()) ** 2).sum()
            final_loss = loss + (self.learn_step_regularization * l2_regularized)
            #print(_, final_loss.item(), loss.item(), l2_regularized.item(), self.learn_step_regularization)
            final_loss.backward()
            #print([p.grad.sum() if p.grad is not None else 'none' for n, p in self.named_parameters()])
            self.optimizer.step()
        #pdb.set_trace()
        if self.learn_step_regularization == 0.0:
            self.regularized_step(l2_cpy)

    def update_l2_exposure(self, l2_exposed):
        for i in l2_exposed:
            self.l2_exposure[i] = self.l2_exposure.get(i, 0.0) + 1.0

    def init_param_freeze(self,):
        for n, p in self.named_parameters():
            p.requires_grad = False
        self.l1_tied_encoder_decoder.init_param_freeze(weight_type='l1')
        self.l2_tied_encoder_decoder.init_param_freeze(weight_type='l2')
        for n, p in self.named_parameters():
            print(n, p.requires_grad)
        return True

    def init_cuda(self,):
        self.context_encoder.init_cuda()
        self.l2_tied_encoder_decoder.init_cuda()
        self.l1_tied_encoder_decoder.init_cuda()
        self = self.cuda()
        return True

    def is_cuda(self,):
        return self.context_encoder.is_cuda()

    def get_l1_weights(self, on_device=True):
        weights = self.l1_tied_encoder_decoder.get_weight(on_device)
        ##TODO: push this function into l2_encoder object
        #if isinstance(self.encoder, torch.nn.Embedding):
        #    weights = self.encoder.weight.clone().detach()
        #else:
        #    raise BaseException("unknown l2_encoder type")
        #if self.is_cuda() and not on_device:
        #    weights = weights.cpu()
        return weights

    def get_l2_weights(self, on_device=True):
        weights = self.l2_tied_encoder_decoder.get_weight(on_device)
        return weights

    def set_l2_weights(self, weights):
        self.l2_tied_encoder_decoder.set_params(weights)
        #if self.is_cuda():
        #    weights = weights.clone().cuda()
        #else:
        #    weights = weights.clone()
        #if isinstance(self.l2_encoder, torch.nn.Embedding):
        #    if isinstance(weights, torch.nn.Parameter):
        #        self.l2_encoder.weight.data = weights.data
        #    elif isinstance(weights, torch.Tensor):
        #        self.l2_encoder.weight.data = weights
        #    else:
        #        raise BaseException("unknown weight type")
        #else:
        #    raise BaseException("unknown l2_encoder type")
        return True