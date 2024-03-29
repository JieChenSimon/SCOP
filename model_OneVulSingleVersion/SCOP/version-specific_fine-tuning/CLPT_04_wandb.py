import torch
from transformers import RobertaTokenizer, RobertaModel
from torch.utils.data import DataLoader, Dataset
from torch import nn
from configparser import ConfigParser
import numpy as np
import logging
import json
import os
import sys
import logging.config
from pytorch_metric_learning.samplers import MPerClassSampler
from tqdm import tqdm
import random
import re
from sklearn.manifold import TSNE
import matplotlib.pyplot as plt
from losses import TripletMSELoss, SupConLoss
from torch.utils.data import DataLoader, Dataset, SequentialSampler, RandomSampler, TensorDataset
from PretrainedEncoder import Model
from tree_sitter import Language, Parser
from parser import (remove_comments_and_docstrings,
                    tree_to_token_index,
                    index_to_code_token,
                    tree_to_variable_index)
from parser import DFG_solidity
from transformers import (WEIGHTS_NAME, AdamW, get_linear_schedule_with_warmup,
                          RobertaConfig, RobertaForSequenceClassification, RobertaTokenizer)
import wandb
cpu_cont = 16
dfg_function = {
    'solidity': DFG_solidity
}

wandb.init(project="CLPR_04")

# load parsers
parsers = {}
for lang in dfg_function:
    LANGUAGE = Language('/home/chen/workspace/codeproject/CL4acrossVersionSC/model_OneVulSingleVersion/ourCLPT/MPCSampler_SupCon/version-specific_fine-tuning/parser/my-languages.so', lang)
    parser = Parser()
    parser.set_language(LANGUAGE)
    parser = [parser, dfg_function[lang]]
    parsers[lang] = parser


# 设置日志
logging.config.fileConfig('/home/chen/workspace/codeproject/CL4acrossVersionSC/model_OneVulSingleVersion/ourCLPT/MPCSampler_SupCon/version-specific_fine-tuning/logging_CLPT_04.cfg')
logger = logging.getLogger('root')

class RuntimeContext(object):
    """ runtime enviroment
    """

    def __init__(self):
        """ initialization
        """
        # configuration initialization
        config_parser = ConfigParser()
        config_file = self.get_config_file_name()
        config_parser.read(config_file, encoding="UTF-8")
        sections = config_parser.sections()

        file_section = sections[0]
        self.train_data_file = config_parser.get(
            file_section, "train_data_file")
        self.output_dir = config_parser.get(file_section, "output_dir")
        self.eval_data_file = config_parser.get(file_section, "eval_data_file")
        self.test_data_file = config_parser.get(file_section, "test_data_file")

        base_section = sections[1]
        self.model_name_or_path = config_parser.get(
            base_section, "model_name_or_path")
        self.config_name = config_parser.get(base_section, "config_name")
        self.tokenizer_name = config_parser.get(base_section, "tokenizer_name")

        parameters_section = sections[2]
        self.code_length = int(config_parser.get(
            parameters_section, "code_length"))
        self.data_flow_length = int(config_parser.get(
            parameters_section, "data_flow_length"))
        self.train_batch_size = int(config_parser.get(
            parameters_section, "train_batch_size"))
        self.eval_batch_size = int(config_parser.get(
            parameters_section, "eval_batch_size"))
        self.gradient_accumulation_steps = int(config_parser.get(
            parameters_section, "gradient_accumulation_steps"))
        self.learning_rate = float(config_parser.get(
            parameters_section, "learning_rate"))
        self.weight_decay = float(config_parser.get(
            parameters_section, "weight_decay"))
        self.adam_epsilon = float(config_parser.get(
            parameters_section, "adam_epsilon"))
        self.max_grad_norm = float(config_parser.get(
            parameters_section, "max_grad_norm"))
        self.max_steps = int(config_parser.get(
            parameters_section, "max_steps"))
        self.warmup_steps = int(config_parser.get(
            parameters_section, "warmup_steps"))
        self.seed = int(config_parser.get(parameters_section, "seed"))
        self.epochs = int(config_parser.get(parameters_section, "epochs"))
        self.n_gpu = torch.cuda.device_count()
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.device = device
        self.sample_per_class = int(config_parser.get(parameters_section, "sample_per_class"))
        self.temperature = float(config_parser.get(parameters_section, "temperature"))
        self.base_temperature = float(config_parser.get(parameters_section, "base_temperature"))
        self.alpha = float(config_parser.get(parameters_section, "alpha"))

    def get_config_file_name(self):
        """ get the configuration file name according to the command line parameters
        """
        argv = sys.argv
        # config_type = "dev"  # default configuration type
        config_type = "devCLPre"  # default configuration type
        if None != argv and len(argv) > 1:
            config_type = argv[1]
        # config_file = config_type + ".cfg"
        config_file = "/home/chen/workspace/codeproject/CL4acrossVersionSC/model_OneVulSingleVersion/ourCLPT/MPCSampler_SupCon/version-specific_fine-tuning/devCLPT_04.cfg"
        logger.info("get_config_file_name() return : " + config_file)
        return config_file
    

# remove comments, tokenize code and extract dataflow
def extract_dataflow(code, parser, lang):
    # remove comments
    try:
        code = remove_comments_and_docstrings(code, lang)
    except:
        pass
    # obtain dataflow
    if lang == "php":
        code = "<?php"+code+"?>"
    try:
        tree = parser[0].parse(bytes(code, 'utf8'))
        root_node = tree.root_node
        tokens_index = tree_to_token_index(root_node)
        code = code.split('\n')
        code_tokens = [index_to_code_token(x, code) for x in tokens_index]
        index_to_code = {}
        for idx, (index, code) in enumerate(zip(tokens_index, code_tokens)):
            index_to_code[index] = (idx, code)
        try:
            DFG, _ = parser[1](root_node, index_to_code, {})
        except:
            DFG = []
        DFG = sorted(DFG, key=lambda x: x[1])
        # identify critical node in DFG
        critical_idx = []
        for id, e in enumerate(DFG):
            if e[0] == "call" and DFG[id+1][0] == "value":
                critical_idx.append(DFG[id-1][1])
                critical_idx.append(DFG[id+2][1])
        lines = []
        for index, code in index_to_code.items():
            if code[0] in critical_idx:
                line = index[0][0]
                lines.append(line)
        lines = list(set(lines))
        for index, code in index_to_code.items():
            if index[0][0] in lines:
                critical_idx.append(code[0])
        critical_idx = list(set(critical_idx))
        max_nums = 0
        cur_nums = -1
        while cur_nums != max_nums and cur_nums != 0:
            max_nums = len(critical_idx)
            for id, e in enumerate(DFG):
                if e[1] in critical_idx:
                    critical_idx += e[-1]
                for i in e[-1]:
                    if i in critical_idx:
                        critical_idx.append(e[1])
                        break
            critical_idx = list(set(critical_idx))
            cur_nums = len(critical_idx)
        dfg = []
        for id, e in enumerate(DFG):
            if e[1] in critical_idx:
                dfg.append(e)
        dfg = sorted(dfg, key=lambda x: x[1])

        # Removing independent points
        indexs = set()
        for d in dfg:
            if len(d[-1]) != 0:
                indexs.add(d[1])
            for x in d[-1]:
                indexs.add(x)
        new_DFG = []
        for d in dfg:
            if d[1] in indexs:
                new_DFG.append(d)
        dfg = new_DFG
    except:
        dfg = []
    return code_tokens, dfg

class InputFeatures(object):
    """A single training/test features for a example."""

    def __init__(self,
                 input_tokens_1,
                 input_ids_1,
                 position_idx_1,
                 dfg_to_code_1,
                 dfg_to_dfg_1,
                 label,
                 url1

                 ):
        # The code function
        self.input_tokens_1 = input_tokens_1
        self.input_ids_1 = input_ids_1
        self.position_idx_1 = position_idx_1
        self.dfg_to_code_1 = dfg_to_code_1
        self.dfg_to_dfg_1 = dfg_to_dfg_1

        # label
        self.label = label
        self.url1 = url1

def convert_examples_to_features(item):
    # source
    url1, label, tokenizer, args, cache, url_to_code = item
    parser = parsers['solidity']

    for url in [url1]:
        if url not in cache:
            func = url_to_code[url]

            # extract data flow
            code_tokens, dfg = extract_dataflow(func, parser, 'solidity')
            code_tokens = [tokenizer.tokenize(
                '@ '+x)[1:] if idx != 0 else tokenizer.tokenize(x) for idx, x in enumerate(code_tokens)]
            ori2cur_pos = {}
            ori2cur_pos[-1] = (0, 0)
            for i in range(len(code_tokens)):
                ori2cur_pos[i] = (ori2cur_pos[i-1][1],
                                  ori2cur_pos[i-1][1]+len(code_tokens[i]))
            code_tokens = [y for x in code_tokens for y in x]

            # truncating
            code_tokens = code_tokens[:args.code_length+args.data_flow_length -
                                      3-min(len(dfg), args.data_flow_length)][:512-3]
            source_tokens = [tokenizer.cls_token] + \
                code_tokens+[tokenizer.sep_token]
            source_ids = tokenizer.convert_tokens_to_ids(source_tokens)
            position_idx = [i+tokenizer.pad_token_id +
                            1 for i in range(len(source_tokens))]
            dfg = dfg[:args.code_length +
                      args.data_flow_length-len(source_tokens)]
            source_tokens += [x[0] for x in dfg]
            position_idx += [0 for x in dfg]
            source_ids += [tokenizer.unk_token_id for x in dfg]
            padding_length = args.code_length + \
                args.data_flow_length-len(source_ids)
            position_idx += [tokenizer.pad_token_id]*padding_length
            source_ids += [tokenizer.pad_token_id]*padding_length

            # reindex
            reverse_index = {}
            for idx, x in enumerate(dfg):
                reverse_index[x[1]] = idx
            for idx, x in enumerate(dfg):
                dfg[idx] = x[:-1]+([reverse_index[i]
                                    for i in x[-1] if i in reverse_index],)
            dfg_to_dfg = [x[-1] for x in dfg]
            dfg_to_code = [ori2cur_pos[x[1]] for x in dfg]
            length = len([tokenizer.cls_token])
            dfg_to_code = [(x[0]+length, x[1]+length) for x in dfg_to_code]
            cache[url] = source_tokens, source_ids, position_idx, dfg_to_code, dfg_to_dfg

    source_tokens_1, source_ids_1, position_idx_1, dfg_to_code_1, dfg_to_dfg_1 = cache[url1]
    return InputFeatures(source_tokens_1, source_ids_1, position_idx_1, dfg_to_code_1, dfg_to_dfg_1,

                         label, url1)

#数据集类
class TextDataset(Dataset):
    def __init__(self, tokenizer, args, file_path='train'):
        self.examples = []
        self.args = args
        index_filename = file_path

        # load index
        logger.info("Creating features from index file at %s ", index_filename)
        url_to_code = {}
        # with open('/'.join(index_filename.split('/')[:-1])+'/data.jsonl') as f:
        # with open('/'.join(index_filename.split('/')[:-1])+'/contracts_data_05.jsonl') as f:
        with open('/home/chen/workspace/codeproject/CL4acrossVersionSC/dataset/data_OneVulCrossVersion/allcontracts04_08.jsonl') as f:
            for line in f:
                line = line.strip()
                js = json.loads(line)
                url_to_code[js['idx']] = js['contract']

        # load code function according to index
        data = []
        cache = {}
        f = open(index_filename)
        with open(index_filename) as f:
            for line in f:
                # line = line.strip()
                # url1, label = line.split('\t')
                url1, label = re.split(r'\s+', line.strip())
                if url1 not in url_to_code:
                    continue  # jump out of for
                if label == '0':
                    label = 0
                else:
                    label = 1
                
                data.append((url1, label, tokenizer, args, cache, url_to_code))
        # only use 10% valid data to keep best model
        if 'valid' in file_path:
            data = random.sample(data, int(len(data)*0.1))
        print('len(data) is: ', len(data))
        # convert example to input features
        self.examples = [convert_examples_to_features(x) for x in tqdm(data, total=len(data))]
        # self.examples = [convert_examples_to_features(x) for x in tqdm(data[:100], total=min(len(data), 1000))]



    def __len__(self):
        return len(self.examples)

    def __getitem__(self, item):
        # calculate graph-guided masked function
        attn_mask_1 = np.zeros((self.args.code_length+self.args.data_flow_length,
                                self.args.code_length+self.args.data_flow_length), dtype=bool)
        # calculate begin index of node and max length of input
        node_index = sum([i > 1 for i in self.examples[item].position_idx_1])
        max_length = sum([i != 1 for i in self.examples[item].position_idx_1])
        # sequence can attend to sequence
        attn_mask_1[:node_index, :node_index] = True
        # special tokens attend to all tokens
        for idx, i in enumerate(self.examples[item].input_ids_1):
            if i in [0, 2]:
                attn_mask_1[idx, :max_length] = True
        # nodes attend to code tokens that are identified from
        for idx, (a, b) in enumerate(self.examples[item].dfg_to_code_1):
            if a < node_index and b < node_index:
                attn_mask_1[idx+node_index, a:b] = True
                attn_mask_1[a:b, idx+node_index] = True
        # nodes attend to adjacent nodes
        for idx, nodes in enumerate(self.examples[item].dfg_to_dfg_1):
            for a in nodes:
                if a+node_index < len(self.examples[item].position_idx_1):
                    attn_mask_1[idx+node_index, a+node_index] = True

        return (torch.tensor(self.examples[item].input_ids_1),
                torch.tensor(self.examples[item].position_idx_1),
                torch.tensor(attn_mask_1),
                torch.tensor(self.examples[item].label))



def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


import torch

def extract_tensors_from_dataset(dataset):
    """
    遍历数据集，并将数据项分解为单独的张量。

    参数:
    dataset: 包含多个数据项的数据集，每个数据项包含四个元素。

    返回:
    input_ids: 输入ID张量。
    position_idxs: 位置索引张量。
    attn_masks: 注意力掩码张量。
    labels: 标签张量。
    """
    # input_ids = []
    # position_idxs = []
    # attn_masks = []
    labels = []

    for item in dataset:
        input_id, position_idx, attn_mask, label = item
        

        # input_ids.append(input_id)
        # position_idxs.append(position_idx)
        # attn_masks.append(attn_mask)
        labels.append(label)
    print("Labels length:", len(labels))  # 打印labels列表的长度
    # return torch.stack(input_ids), torch.stack(position_idxs), torch.stack(attn_masks), torch.stack(labels)
    return torch.stack(labels)

# 使用示例
# 假设 train_data 是您的数据集



def train(args, train_dataset, model, tokenizer):
    """ Train the model """

    
    y_train_labels = extract_tensors_from_dataset(train_dataset)
    print('the number of train_dataset is: ', len(train_dataset))
    print('the length of y_train_labels is: ', len(y_train_labels))
    print('the first to tenth y_train_labels are: ', y_train_labels[:10])
    print('args.sample_per_class is: ', args.sample_per_class)
    print('args.train_batch_size is: ', args.train_batch_size)
    # build dataloader
    
    # sample_per_class是number of samples per class per batch\
    sampler_MPC = MPerClassSampler(y_train_labels, args.sample_per_class)
    print('sampler_MPC is: ', sampler_MPC)
    train_data_loader = DataLoader(dataset=train_dataset, batch_size=args.train_batch_size, sampler=sampler_MPC)
    print('the length of train_data_loader after sampled is: ', len(train_data_loader))

    #     # 获取第一个批次
    # first_batch = next(iter(train_data_loader))

    # # 检查批次结构
    # print(type(first_batch))
    # print(len(first_batch))

    # for batch in train_data_loader:
    #     # 假设每个批次的结构是 (data, label)，或者根据您的实际批次结构进行调整
    #     data, position_idx, attn_mask, labels = batch

    #     # 打印标签
    #     print(labels)



    # train_sampler = RandomSampler(train_dataset)
    # train_dataloader = DataLoader(
    #     train_dataset, sampler=train_sampler, batch_size=args.train_batch_size, num_workers=4)

    # args.max_steps = args.epochs*len(train_dataloader)
    args.max_steps = args.epochs*len(train_data_loader)
    # args.save_steps = len(train_dataloader)//10
    args.save_steps = len(train_data_loader)//10
    args.warmup_steps = args.max_steps//5
    model.to(args.device)

    # Prepare optimizer and schedule (linear warmup and decay)
    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
         'weight_decay': args.weight_decay},
        {'params': [p for n, p in model.named_parameters() if any(
            nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]
    optimizer = AdamW(optimizer_grouped_parameters,
                      lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=args.warmup_steps,
                                                num_training_steps=args.max_steps)

    # multi-gpu training
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", args.epochs)
    logger.info("  Instantaneous batch size per GPU = %d",
                args.train_batch_size//args.n_gpu)
    logger.info("  Total train batch size = %d",
                args.train_batch_size*args.gradient_accumulation_steps)
    logger.info("  Gradient Accumulation steps = %d",
                args.gradient_accumulation_steps)
    logger.info("  Total optimization steps = %d", args.max_steps)

    global_step = 0
    tr_loss, logging_loss, avg_loss, tr_nb, tr_num, train_loss = 0.0, 0.0, 0.0, 0, 0, 0
    best_f1 = 0

    model.zero_grad()
    print('args.temperature is: ', args.temperature)
    print('args.temperature is: ', args.base_temperature)
    supcon_loss = SupConLoss(temperature=args.temperature, base_temperature=args.base_temperature).to(args.device)

    for idx in range(args.epochs):
        bar = tqdm(train_data_loader, total=len(train_data_loader))
        tr_num = 0
        train_loss = 0
        for step, batch in enumerate(bar):
            (inputs_ids_1, position_idx_1, attn_mask_1,
             labels) = [x.to(args.device) for x in batch]
            model.train()
            loss_CrossEntropy, logits, encoder_embedding = model(inputs_ids_1, position_idx_1, attn_mask_1, labels)
            # print('encoder_embedding is: ', encoder_embedding)
            encoder_embedding = encoder_embedding.unsqueeze(1)
            loss_SupCon = supcon_loss(encoder_embedding, labels)


            if args.n_gpu > 1:
                loss_CrossEntropy = loss_CrossEntropy.mean()
                proportion = args.alpha * loss_CrossEntropy.detach().data.item() / (loss_CrossEntropy.detach().data.item() + loss_SupCon.detach().data.item())

            # print('proportion is: ', proportion)
            loss = loss_CrossEntropy + proportion*loss_SupCon

            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps

            loss.backward()
            torch.nn.utils.clip_grad_norm_(
                model.parameters(), args.max_grad_norm)

            tr_loss += loss.item()
            tr_num += 1
            train_loss += loss.item()
            if avg_loss == 0:
                avg_loss = tr_loss

            avg_loss = round(train_loss/tr_num, 5)
            bar.set_description("epoch {} loss {}".format(idx, avg_loss))
            wandb.log({"train_loss": avg_loss, "epoch": idx})

            if (step + 1) % args.gradient_accumulation_steps == 0:
                optimizer.step()
                optimizer.zero_grad()
                scheduler.step()
                global_step += 1
                output_flag = True
                avg_loss = round(
                    np.exp((tr_loss - logging_loss) / (global_step - tr_nb)), 4)

                
                if global_step % args.save_steps == 0:
                    results = evaluate(args, model, tokenizer,
                                       eval_when_training=True)

                    # Save model checkpoint
                    if results['eval_f1'] > best_f1:
                        best_f1 = results['eval_f1']
                        logger.info("  "+"*"*20)
                        logger.info("  Best f1:%s", round(best_f1, 4))
                        logger.info("  "+"*"*20)

                        checkpoint_prefix = 'checkpoint-best-f1'
                        output_dir = os.path.join(
                            args.output_dir, '{}'.format(checkpoint_prefix))
                        if not os.path.exists(output_dir):
                            os.makedirs(output_dir)
                        model_to_save = model.module if hasattr(
                            model, 'module') else model
                        output_dir = os.path.join(
                            output_dir, '{}'.format('model.bin'))
                        torch.save(model_to_save.state_dict(), output_dir)
                        logger.info(
                            "Saving model checkpoint to %s", output_dir)


def evaluate(args, model, tokenizer, eval_when_training=False):
    # build dataloader
    eval_dataset = TextDataset(tokenizer, args, file_path=args.eval_data_file)
    eval_sampler = SequentialSampler(eval_dataset)
    eval_dataloader = DataLoader(
        eval_dataset, sampler=eval_sampler, batch_size=args.eval_batch_size, num_workers=4)

    # multi-gpu evaluate
    if args.n_gpu > 1 and eval_when_training is False:
        model = torch.nn.DataParallel(model)

    # Eval!
    logger.info("***** Running evaluation *****")
    logger.info("  Num examples = %d", len(eval_dataset))
    logger.info("  Batch size = %d", args.eval_batch_size)

    eval_loss = 0.0
    nb_eval_steps = 0
    model.eval()
    logits = []
    y_trues = []
    # for batch in eval_dataloader:
    #     (inputs_ids_1, position_idx_1, attn_mask_1,
    #      labels) = [x.to(args.device) for x in batch]
    #     with torch.no_grad():
    #         lm_loss_CrossEntropy, logit, encoder_embedding = model(
    #             inputs_ids_1, position_idx_1, attn_mask_1, labels)
    #         eval_loss += lm_loss_CrossEntropy.mean().item()
    #         logits.append(logit.cpu().numpy())
    #         y_trues.append(labels.cpu().numpy())
    #     nb_eval_steps += 1

    supcon_loss = SupConLoss(temperature=args.temperature, base_temperature=args.base_temperature).to(args.device)
    for batch in eval_dataloader:(inputs_ids_1, position_idx_1, attn_mask_1, labels) = [x.to(args.device) for x in batch]
    with torch.no_grad():

        lm_loss_CrossEntropy, logit, encoder_embedding = model(inputs_ids_1, position_idx_1, attn_mask_1, labels)
        eval_lm_loss_CrossEntropy = lm_loss_CrossEntropy.mean()

        loss_SupCon = supcon_loss(encoder_embedding, labels)

        

        proportion = args.alpha * eval_lm_loss_CrossEntropy.detach().data.item() / (eval_lm_loss_CrossEntropy.detach().data.item() + loss_SupCon.detach().data.item())
        loss_SupCon = supcon_loss(encoder_embedding.unsqueeze(1), labels)
        

        
        # Assuming you have the same weighting proportion for evaluation as well
        eval_loss += eval_lm_loss_CrossEntropy + proportion * loss_SupCon
        
        logits.append(logit.cpu().numpy())
        y_trues.append(labels.cpu().numpy())
    
    nb_eval_steps += 1


    # calculate scores
    logits = np.concatenate(logits, 0)

    y_trues = np.concatenate(y_trues, 0)
    best_threshold = 0.5
    best_f1 = 0

    y_scores = logits
    y_preds = logits[:, 1] > best_threshold

    #计算TP,FP,TN,FN
    TP = np.sum((y_trues==1) & (y_preds==1))
    FP = np.sum((y_trues==0) & (y_preds==1))
    TN = np.sum((y_trues==0) & (y_preds==0))
    FN = np.sum((y_trues==1) & (y_preds==0))

    from sklearn.metrics import recall_score
    recall = recall_score(y_trues, y_preds, average='macro')
    from sklearn.metrics import precision_score
    precision = precision_score(y_trues, y_preds, average='macro')
    from sklearn.metrics import f1_score
    f1 = f1_score(y_trues, y_preds, average='macro')
    macro_f1 = 2 * (precision * recall) / (precision + recall)

    #acc
    acc = (TP+TN)/(TP+TN+FP+FN)

    #计算TPR和FPR,使用tp，fp，tn，fn计算
    TPR = TP / (TP+FN)
    FPR = FP / (FP+TN)

    #使用TPR和FPR计算AUC
    from sklearn.metrics import roc_curve, roc_auc_score, auc
    


    
    wandb.log({"eval_roc_curve": wandb.plot.roc_curve(y_trues, y_scores, labels=None)})


    result = {
        "eval_recall": float(recall),
        "eval_precision": float(precision),
        "eval_f1": float(f1),
        "eval_f1_macro": float(macro_f1),
        "eval_acc": float(acc),
        "eval_threshold": best_threshold,
        "eval_TP": int(TP),
        "eval_FP": int(FP),
        "eval_TN": int(TN),
        "eval_FN": int(FN),
        "eval_TPR": float(TPR),
        "eval_FPR": float(FPR)
    }

    logger.info("***** Eval results *****")
    for key in sorted(result.keys()):
        logger.info("  %s = %s", key, str(round(result[key], 4)))
        
        wandb.log({key: result[key]})

    return result


def test(args, model, tokenizer, best_threshold=0):
    # build dataloader
    test_dataset = TextDataset(tokenizer, args, file_path=args.test_data_file)
    test_sampler = SequentialSampler(test_dataset)
    test_dataloader = DataLoader(
        test_dataset, sampler=test_sampler, batch_size=args.eval_batch_size, num_workers=4)

    # multi-gpu evaluate
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Eval!
    logger.info("***** Running Test *****")
    logger.info("  Num examples = %d", len(test_dataloader))
    logger.info("  Batch size = %d", args.eval_batch_size)
    eval_loss = 0.0
    nb_eval_steps = 0
    model.eval()
    logits = []
    y_trues = []
    supcon_loss = SupConLoss(temperature=args.temperature, base_temperature=args.base_temperature).to(args.device)
    for batch in tqdm(test_dataloader, total=len(test_dataloader)):
        (inputs_ids_1, position_idx_1, attn_mask_1,
         labels) = [x.to(args.device) for x in batch]
        with torch.no_grad():
            lm_loss_CrossEntropy, logit, encoder_embedding = model(inputs_ids_1, position_idx_1, attn_mask_1, labels)

        
            loss_SupCon = supcon_loss(encoder_embedding.unsqueeze(1), labels)
        
            test_loss_CrossEntropy = lm_loss_CrossEntropy.mean()
            
            proportion = args.alpha * test_loss_CrossEntropy.detach().data.item() / (test_loss_CrossEntropy.detach().data.item() + loss_SupCon.detach().data.item())

            
            eval_loss += test_loss_CrossEntropy + proportion * loss_SupCon
            logits.append(logit.cpu().numpy())
            y_trues.append(labels.cpu().numpy())
        nb_eval_steps += 1


    # output result
    logits = np.concatenate(logits, 0)
    y_trues = np.concatenate(y_trues, 0)
    y_preds = logits[:, 1] > best_threshold
    y_scores = logits
    with open(os.path.join(args.output_dir, "predictions.txt"), 'w') as f:
        for example, pred in zip(test_dataset.examples, y_preds):
            if pred:
                f.write(example.url1+'\t'+'1'+'\n')
            else:
                f.write(example.url1+'\t'+'0'+'\n')

    #计算TP,FP,TN,FN
    TP = np.sum((y_trues==1) & (y_preds==1))
    FP = np.sum((y_trues==0) & (y_preds==1))
    TN = np.sum((y_trues==0) & (y_preds==0))
    FN = np.sum((y_trues==1) & (y_preds==0))

    # calculate scores
    from sklearn.metrics import recall_score
    recall = recall_score(y_trues, y_preds, average='macro')
    from sklearn.metrics import precision_score
    precision = precision_score(y_trues, y_preds, average='macro')
    from sklearn.metrics import f1_score
    f1 = f1_score(y_trues, y_preds, average='macro')
    macro_f1 = 2 * (precision * recall) / (precision + recall)

    #acc
    acc = (TP+TN)/(TP+TN+FP+FN)
    #计算TPR和FPR,使用tp，fp，tn，fn计算
    TPR = TP / (TP+FN)
    FPR = FP / (FP+TN)


    wandb.log({"test_roc_curve": wandb.plot.roc_curve(y_trues, y_scores, labels=None)})


    result = {
        "test_recall_sklearn": float(recall),
        "test_precision_sklearn": float(precision),
        "test_f1": float(f1),
        "test_f1_macro": float(macro_f1),
        "test_acc": float(acc),
        "test_threshold": best_threshold,
        "test_TP": int(TP),
        "test_FP": int(FP),
        "test_TN": int(TN),
        "test_FN": int(FN),
        "test_TPR": float(TPR),
        "test_FPR": float(FPR)
    }

    logger.info("***** Test results *****")
    for key in sorted(result.keys()):
        logger.info("  %s = %s", key, str(round(result[key], 4)))
        
        wandb.log({key: result[key]})

    return result


def main():
    args = RuntimeContext()
        # Setup logging
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S', level=logging.INFO)
    logger.warning("device: %s, n_gpu: %s", args.device, args.n_gpu,)
    # Set seed
    set_seed(args)

    config = RobertaConfig.from_pretrained(
        args.config_name if args.config_name else args.model_name_or_path)
    # 数据加载和预处理（需根据你的数据集格式调整）
    # tokenizer = RobertaTokenizer.from_pretrained('microsoft/graphcodebert-base')
    tokenizer = RobertaTokenizer.from_pretrained("/home/chen/workspace/codeproject/CL4acrossVersionSC/modelSingleVulSingleVersion/pretrained_model/graphcodebert-base")
    # encodings = tokenizer(['code_snippet_1', 'code_snippet_2', ...], truncation=True, padding=True)
    model = RobertaForSequenceClassification.from_pretrained(
        args.model_name_or_path, config=config)
    model = Model(model, config, tokenizer, args)

    train_dataset = TextDataset(
        tokenizer, args, file_path=args.train_data_file)

    train(args, train_dataset, model, tokenizer)

    checkpoint_prefix = 'checkpoint-best-f1/model.bin'
    output_dir = os.path.join(args.output_dir, '{}'.format(checkpoint_prefix))
    model.load_state_dict(torch.load(output_dir))
    model.to(args.device)
    test(args, model, tokenizer, best_threshold=0.5)



if __name__ == "__main__":
    main()



