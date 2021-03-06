#!/usr/bin/env python
import argparse
import numpy as np
import pickle
import sys
import torch
import random
import time

from src.models.model import CBiLSTM
from src.models.model import CBiLSTMFast
from src.models.map_model import CBiLSTMFastMap
from src.models.model import CTransformerEncoder
from src.models.model import VarEmbedding
from src.models.model import WordRepresenter
from src.states.states import MacaronicState
from src.states.states import PriorityQ
from src.states.states import MacaronicSentence
from src.states.mcts_states import Game
from src.states.mcts_states import SearchNode
from src.states.mcts_states import SearchTree
from search import apply_swap
from search import beam_search
from search import beam_search_per_sentence
from search import random_walk
from search import make_start_state
from train import make_cl_decoder
from train import make_cl_encoder
from train import make_wl_decoder
from train import make_wl_encoder
from train import make_random_mask
import time


from src.utils.utils import ParallelTextDataset
from src.utils.utils import SPECIAL_TOKENS


if __name__ == '__main__':
    print(sys.stdout.encoding)
    opt = argparse.ArgumentParser(description="write program description here")
    # insert options here
    opt.add_argument('--parallel_corpus', action='store', dest='parallel_corpus', required=True)
    opt.add_argument('--v2i', action='store', dest='v2i', required=True,
                     help='vocab to index pickle obj')
    opt.add_argument('--v2spell', action='store', dest='v2spell', required=False,
                     help='vocab to spelling pickle obj')
    opt.add_argument('--c2i', action='store', dest='c2i', required=False,
                     help='character (corpus and gloss)  to index pickle obj')
    opt.add_argument('--gv2i', action='store', dest='gv2i', required=False, default=None,
                     help='gloss vocab to index pickle obj')
    opt.add_argument('--gv2spell', action='store', dest='gv2spell', required=False, default=None,
                     help='gloss vocab to index pickle obj')
    opt.add_argument('--batch_size', action='store', type=int, dest='batch_size', default=1)
    opt.add_argument('--gpuid', action='store', type=int, dest='gpuid', default=-1)
    opt.add_argument('--cloze_model', action='store', dest='cloze_model', required=True)
    opt.add_argument('--stochastic', action='store', dest='stochastic', default=0, type=int, choices=[0, 1])
    opt.add_argument('--joined_l2_l1', action='store', dest='joined_l2_l1', default=0, type=int, choices=[0, 1])
    opt.add_argument('--mask_unseen_l2', action='store', dest='mask_unseen_l2', default=1, type=int, choices=[0, 1])
    opt.add_argument('--swap_limit', action='store', dest='swap_limit', default=0.3, type=float)
    opt.add_argument('--key', action='store', dest='key', required=True)
    opt.add_argument('--penalty', action='store', dest='penalty', default=0.2, type=float)
    opt.add_argument('--binary_branching', action='store', dest='binary_branching', default=0, type=int, choices=[0, 1])
    opt.add_argument('--beam_size', action='store', dest='beam_size', default=10, type=int)
    opt.add_argument('--max_steps', action='store', dest='max_steps', default=100, type=int)
    opt.add_argument('--improvement', action='store', dest='improvement_threshold', default=0.01, type=float)
    opt.add_argument('--backup_type', action='store', dest='backup_type', default='ave',
                     type=str, choices=['ave', 'max'])
    opt.add_argument('--max_search_depth', action='store', dest='max_search_depth', default=1000, type=int)
    opt.add_argument('--rollout_function', action='store', dest='rollout_function', default='beam_search', type=str,
                     choices=['beam_search', 'random_walk', 'beam_search_per_sentence'])
    opt.add_argument('--rollout_binary_branching', action='store', dest='rollout_binary_branching', default=1, type=int,
                     choices=[0, 1])
    opt.add_argument('--verbose', action='store_true', dest='verbose', default=False)
    opt.add_argument('--seed', action='store', dest='seed', default=1234, type=int)

    options = opt.parse_args()
    print(options)
    torch.manual_seed(options.seed)
    random.seed(options.seed)
    np.random.seed(options.seed)
    if options.gpuid > -1:
        torch.cuda.set_device(options.gpuid)
        tmp = torch.ByteTensor([0])
        tmp.cuda()
        print("using GPU", options.gpuid)
    else:
        print("using CPU")

    v2i = pickle.load(open(options.v2i, 'rb'))
    i2v = dict((v, k) for k, v in v2i.items())
    gv2i = pickle.load(open(options.gv2i, 'rb')) if options.gv2i is not None else None
    i2gv = dict((v, k) for k, v in gv2i.items())
    l1_key, l2_key = zip(*pickle.load(open(options.key, 'rb')))
    l2_key = torch.LongTensor(list(l2_key))
    l1_key = torch.LongTensor(list(l1_key))
    dataset = ParallelTextDataset(options.parallel_corpus, v2i, gv2i)
    v_max_vocab = len(v2i)
    g_max_vocab = len(gv2i)
    cloze_model = torch.load(options.cloze_model, map_location=lambda storage, loc: storage)

    if isinstance(cloze_model.encoder, VarEmbedding):
        gv2c, c2i = None, None
        wr = cloze_model.encoder.word_representer
        we_size = wr.we_size
        learned_l1_weights = cloze_model.encoder.word_representer()
        g_wr = WordRepresenter(gv2c, c2i, len(c2i), wr.ce_size,
                               c2i[SPECIAL_TOKENS.PAD], wr.cr_size, we_size,
                               bidirectional=wr.bidirectional, dropout=wr.dropout,
                               num_required_vocab=max(v_max_vocab, g_max_vocab))
        for (name_op, op), (name_p, p) in zip(wr.named_parameters(), g_wr.named_parameters()):
            assert name_p == name_op
            if name_op == 'extra_ce_layer.weight':
                pass
            else:
                p.data.copy_(op.data)
        g_wr.set_extra_feat_learnable(True)
        if options.gpuid > -1:
            g_wr.init_cuda()
        g_cl_encoder = make_cl_encoder(g_wr)
        g_cl_decoder = make_cl_decoder(g_wr)
        encoder = make_wl_encoder(None, None, learned_l1_weights.data.clone())
        decoder = make_wl_decoder(encoder)
        cloze_model.encoder = encoder
        cloze_model.decoder = decoder
        cloze_model.l2_encoder = g_cl_encoder
        cloze_model.l2_decoder = g_cl_decoder
        cloze_model.init_param_freeze(CBiLSTM.L2_LEARNING)
    elif isinstance(cloze_model, CBiLSTM) or isinstance(cloze_model, CBiLSTMFast):
        learned_l1_weights = cloze_model.encoder.weight.data.clone()
        we_size = cloze_model.encoder.weight.size(1)
        encoder = make_wl_encoder(None, None, learned_l1_weights)
        decoder = make_wl_decoder(encoder)
        g_wl_encoder = make_wl_encoder(g_max_vocab, we_size, None)
        g_wl_decoder = make_wl_decoder(g_wl_encoder)
        cloze_model.encoder = encoder
        cloze_model.decoder = decoder
        cloze_model.l2_encoder = g_wl_encoder
        cloze_model.l2_decoder = g_wl_decoder
        cloze_model.init_param_freeze(CBiLSTM.L2_LEARNING)
    elif isinstance(cloze_model, CBiLSTMFastMap):
        print('here')
        learned_l1_weights = cloze_model.encoder.weight.data.clone()
        we_size = cloze_model.encoder.weight.size(1)
        encoder = make_wl_encoder(None, None, learned_l1_weights)
        decoder = make_wl_decoder(encoder)
        g_wl_encoder = make_wl_encoder(g_max_vocab, we_size, None)
        g_wl_decoder = make_wl_decoder(g_wl_encoder)
        cloze_model.encoder = encoder
        cloze_model.decoder = decoder
        map_weights = torch.FloatTensor(g_max_vocab, v_max_vocab).uniform_(-0.01, 0.01)
        cloze_model.init_l2_weights(map_weights)
        cloze_model.init_param_freeze(CBiLSTM.L2_LEARNING)
    elif isinstance(cloze_model, CTransformerEncoder):
        learned_weights = cloze_model.encoder.weight.data.clone()
        we_size = cloze_model.encoder.weight.size(1)
        encoder = make_wl_encoder(None, None, learned_weights)
        decoder = make_wl_decoder(encoder)
        g_wl_encoder = make_wl_encoder(g_max_vocab, we_size, None)
        g_wl_decoder = make_wl_decoder(g_wl_encoder)
        cloze_model.encoder = encoder
        cloze_model.decoder = decoder
        cloze_model.l2_encoder = g_wl_encoder
        cloze_model.l2_decoder = g_wl_decoder
        cloze_model.init_param_freeze(CBiLSTM.L2_LEARNING)
    else:
        raise NotImplementedError("unknown cloze_model" + str(type(cloze_model)))

    if options.joined_l2_l1:
        assert not isinstance(cloze_model, CBiLSTMFastMap)
        cloze_model.join_l2_weights()
    cloze_model.mask_unseen_l2 = options.mask_unseen_l2

    if options.gpuid > -1:
        cloze_model.init_cuda()
    cloze_model.set_key(l1_key, l2_key)
    cloze_model.init_key()
    cloze_model.l2_dict = gv2i
    if isinstance(cloze_model, CBiLSTM) or \
       isinstance(cloze_model, CBiLSTMFast) or \
       isinstance(cloze_model, CBiLSTMFastMap):
        cloze_model.train()

    print(cloze_model)
    macaronic_sents = []
    weights = cloze_model.get_weight()
    init_weights = weights.clone()
    kwargs = vars(options)
    kwargs['max_search_depth'] = 100
    game = Game(cloze_model, apply_swap, opt=kwargs)
    if options.rollout_function == 'random_walk':
        search_tree = SearchTree(game, options.backup_type, random_walk, kwargs, apply_swap)
    elif options.rollout_function == 'beam_search':
        search_tree = SearchTree(game, options.backup_type, beam_search, kwargs, apply_swap)
    elif options.rollout_function == 'beam_search_per_sentence':
        search_tree = SearchTree(game, options.backup_type, beam_search_per_sentence, kwargs, apply_swap)
    else:
        raise BaseException("unknown rollout_function")
    start_state = make_start_state(i2v, i2gv, init_weights, cloze_model, dataset, **kwargs)
    possible_actions, possible_action_weights = start_state.possible_actions()
    unexpanded_actions = {a: None for a in possible_actions}
    expanded_actions = {}
    root_node = SearchNode(start_state, unexpanded_actions, expanded_actions)
    terminal_node = search_tree.recursive_search(root_node)
    print('completed mcts')
    print(str(terminal_node))
