import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import numpy as np
from tqdm import tqdm
from sklearn.metrics import roc_auc_score, accuracy_score, mean_squared_error, f1_score
from torch.utils.data import DataLoader, TensorDataset, random_split

class ConvolutionalTransform2(nn.Module):
    def __init__(self, fc_out_features, input_channels=3, output_channels=1, kernel_size=1, stride=1, padding=0):
        super(ConvolutionalTransform2, self).__init__()
        self.conv1 = nn.Conv1d(input_channels, output_channels, kernel_size, stride, padding)

    def forward(self, x):
        x = self.conv1(x)
        x = x.view(x.size(0), -1)
        return x

class Transform_Exr_Stu(nn.Module):
    def __init__(self, pp_dim, s_ranges):
        super(Transform_Exr_Stu, self).__init__()
        self.s_exer_vectors = nn.ParameterList([nn.Parameter(torch.rand(pp_dim)) for _ in range(len(s_ranges))])
        self.Conv1 = ConvolutionalTransform2(pp_dim,input_channels=len(s_ranges))

    def forward(self, x):
        exr_vector = torch.cat([vector.unsqueeze(0) for vector in self.s_exer_vectors], dim=0)
        new_exr_vector = self.Conv1(exr_vector)
        new_exr_vector = torch.cat([new_exr_vector.expand(x.size(0), -1), x], dim=1)

        return new_exr_vector

class Source_Net(nn.Module):
    def __init__(self, knowledge_n, exer_n, student_n, low_dim, pp_dim, s_ranges):
        self.knowledge_n = knowledge_n
        self.exer_n = exer_n
        self.stu_n = student_n
        self.low_dim = low_dim
        self.pp_dim = pp_dim
        self.s_ranges = s_ranges
        self.net1 = knowledge_n
        self.prednet_input_len = self.knowledge_n
        self.prednet_len1, self.prednet_len2 = 512, 256

        super(Source_Net, self).__init__()

        self.k_difficulty = nn.ParameterList([nn.Parameter(torch.randn(self.exer_n, self.low_dim))
                                       for _ in range(len(s_ranges))])
        for k_difficulty in self.k_difficulty:
            nn.init.xavier_uniform_(k_difficulty)
        self.prompt_k = nn.Parameter(torch.randn(self.exer_n, self.pp_dim))
        nn.init.xavier_uniform_(self.prompt_k)

        self.student_emb = nn.Parameter(torch.rand((self.stu_n, self.low_dim)))
        nn.init.xavier_uniform_(self.student_emb)
        self.s_stu_vectors = nn.ParameterList([nn.Parameter(torch.rand(self.pp_dim)) for _ in range(len(s_ranges))])

        self.knowledge_emb = nn.Embedding(self.knowledge_n, self.low_dim)
        self.k_index = torch.LongTensor(list(range(self.knowledge_n)))

        self.prednet_full1 = nn.Linear(self.knowledge_n + self.low_dim, self.net1, bias=False)
        self.drop_1 = nn.Dropout(p=0.5)
        self.prednet_full2 = nn.Linear(self.knowledge_n + self.low_dim, self.net1, bias=False)
        self.drop_2 = nn.Dropout(p=0.5)
        self.prednet_full3 = nn.Linear(1 * self.net1, 1)
        self.layer1 = nn.Linear(self.low_dim, 1)

        # initialization
        for name, param in self.named_parameters():
            if 'weight' in name:
                nn.init.xavier_normal_(param)

        self.fc1 = nn.Linear(self.pp_dim + self.knowledge_n, self.knowledge_n)
        self.fc2 = nn.Linear(self.pp_dim + self.knowledge_n, self.knowledge_n)

    def forward(self, stu_id, exer_id, kn_emb):

        knowledge_low_emb = self.knowledge_emb(self.k_index)
        # step2.
        #-----------------------------------------------------
        prompt_k_repeated = self.prompt_k.repeat(len(self.s_ranges), 1)
        k_concatenated = torch.cat([k for k in self.k_difficulty], dim=0)
        prompt_k = torch.index_select(prompt_k_repeated, dim=0, index=exer_id)
        old_k = torch.index_select(k_concatenated, dim=0, index=exer_id)
        old_k = torch.sigmoid(torch.mm(old_k, knowledge_low_emb.T))
        new_k = torch.cat([prompt_k, old_k], dim=1)
        batch_exer_emb = torch.sigmoid(self.fc1(new_k))
        #-----------------------------------------------------
        batch_exer_vector = batch_exer_emb.repeat(1, self.knowledge_n).reshape(batch_exer_emb.shape[0], self.knowledge_n,
                                                                               batch_exer_emb.shape[1])

        #------------------------------------------------------------------
        temp_vectors = torch.cat(
            [vector.repeat(r[1] - r[0] + 1, 1) for vector, r in zip(self.s_stu_vectors, self.s_ranges)], dim=0)
        prompt_stu = torch.sigmoid(torch.index_select(temp_vectors, dim=0, index=stu_id))
        old_stu = torch.sigmoid(torch.index_select(self.student_emb, dim=0, index=stu_id))
        old_stu = torch.sigmoid(torch.mm(old_stu, knowledge_low_emb.T))
        new_stu = torch.cat([prompt_stu, old_stu], dim=1)
        batch_stu_emb = torch.sigmoid(self.fc2(new_stu))

        #------------------------------------------------------------------
        batch_stu_vector = batch_stu_emb.repeat(1, self.knowledge_n).reshape(batch_stu_emb.shape[0], self.knowledge_n,
                                                                             batch_stu_emb.shape[1])

        kn_vector = knowledge_low_emb.repeat(batch_stu_emb.shape[0], 1).reshape(batch_stu_emb.shape[0],
                                                                                knowledge_low_emb.shape[0],
                                                                                knowledge_low_emb.shape[1])

        preference = torch.sigmoid(self.prednet_full1(torch.cat((batch_stu_vector, kn_vector), dim=2)))
        diff = torch.sigmoid(self.prednet_full2(torch.cat((batch_exer_vector, kn_vector), dim=2)))
        o = torch.sigmoid(self.prednet_full3(preference - diff))

        sum_out = torch.sum(o * kn_emb.unsqueeze(2), dim=1)
        count_of_concept = torch.sum(kn_emb, dim=1).unsqueeze(1)
        output = sum_out / count_of_concept
        return output

class Target_Net(nn.Module):
    def __init__(self, knowledge_n, exer_n, student_n, low_dim, pp_dim, s_ranges):
        self.knowledge_n = knowledge_n
        self.exer_n = exer_n
        self.stu_n = student_n
        self.low_dim = low_dim
        self.pp_dim = pp_dim
        self.s_ranges = s_ranges
        self.net1 = knowledge_n
        self.prednet_input_len = self.knowledge_n
        self.prednet_len1, self.prednet_len2 = 512, 256

        super(Target_Net, self).__init__()

        self.k_difficulty = nn.Embedding(self.exer_n, self.low_dim)
        self.prompt_k = nn.Embedding(self.exer_n, self.pp_dim)

        self.knowledge_emb = nn.Embedding(self.knowledge_n, self.low_dim)
        self.k_index = torch.LongTensor(list(range(self.knowledge_n)))

        self.student_emb = nn.Embedding(self.stu_n, self.low_dim)
        self.transform_layer_stu = Transform_Exr_Stu(self.pp_dim, self.s_ranges)

        self.prednet_full1 = nn.Linear(self.knowledge_n + self.low_dim, self.net1, bias=False)
        self.drop_1 = nn.Dropout(p=0.5)
        self.prednet_full2 = nn.Linear(self.knowledge_n + self.low_dim, self.net1, bias=False)
        self.drop_2 = nn.Dropout(p=0.5)
        self.prednet_full3 = nn.Linear(1 * self.net1, 1)
        self.layer1 = nn.Linear(self.low_dim, 1)

        for name, param in self.named_parameters():
            if 'weight' in name:
                nn.init.xavier_normal_(param)

        self.fc1 = nn.Linear(self.pp_dim + self.knowledge_n, self.knowledge_n)
        self.fc2 = nn.Linear(self.pp_dim + self.knowledge_n, self.knowledge_n)

    def forward(self, stu_id, exer_id, kn_emb):
        knowledge_low_emb = self.knowledge_emb(self.k_index)
        #-----------------------------------------------------
        prompt_k = self.prompt_k(exer_id)
        old_k = self.k_difficulty(exer_id)
        old_k = torch.sigmoid(torch.mm(old_k, knowledge_low_emb.T))
        new_k = torch.cat([prompt_k,old_k],dim=1)
        batch_exer_emb = torch.sigmoid(self.fc1(new_k))
        #-----------------------------------------------------
        batch_exer_vector = batch_exer_emb.repeat(1, self.knowledge_n).reshape(batch_exer_emb.shape[0], self.knowledge_n, batch_exer_emb.shape[1])

        #------------------------------------------------------------------
        old_stu = self.student_emb(stu_id)
        old_stu = torch.sigmoid(torch.mm(old_stu, knowledge_low_emb.T))
        new_stu = self.transform_layer_stu(old_stu)
        batch_stu_emb = torch.sigmoid(self.fc2(new_stu))
        #------------------------------------------------------------------
        batch_stu_vector = batch_stu_emb.repeat(1, self.knowledge_n).reshape(batch_stu_emb.shape[0], self.knowledge_n,
                                                                             batch_stu_emb.shape[1])

        kn_vector = knowledge_low_emb.repeat(batch_stu_emb.shape[0], 1).reshape(batch_stu_emb.shape[0],
                                                                                knowledge_low_emb.shape[0],
                                                                                knowledge_low_emb.shape[1])

        preference = torch.sigmoid(self.prednet_full1(torch.cat((batch_stu_vector, kn_vector), dim=2)))
        diff = torch.sigmoid(self.prednet_full2(torch.cat((batch_exer_vector, kn_vector), dim=2)))
        o = torch.sigmoid(self.prednet_full3(preference - diff))

        sum_out = torch.sum(o * kn_emb.unsqueeze(2), dim=1)
        count_of_concept = torch.sum(kn_emb, dim=1).unsqueeze(1)
        output = sum_out / count_of_concept
        return output

class Target_Net2(nn.Module):
    def __init__(self, knowledge_n, exer_n, student_n, low_dim, pp_dim, s_ranges):
        self.knowledge_n = knowledge_n
        self.exer_n = exer_n
        self.stu_n = student_n
        self.low_dim = low_dim
        self.pp_dim = pp_dim
        self.s_ranges = s_ranges
        self.net1 = knowledge_n
        self.prednet_input_len = self.knowledge_n
        self.prednet_len1, self.prednet_len2 = 512, 256

        super(Target_Net2, self).__init__()

        self.prompt_k = nn.Embedding(self.exer_n, self.pp_dim)
        self.generalize_layer_k = nn.Linear(self.pp_dim, self.low_dim)

        self.knowledge_emb = nn.Embedding(self.knowledge_n, self.low_dim)
        self.k_index = torch.LongTensor(list(range(self.knowledge_n)))

        self.student_emb = nn.Embedding(self.stu_n, self.low_dim)
        self.transform_layer_stu = Transform_Exr_Stu(self.pp_dim, self.s_ranges)

        self.prednet_full1 = nn.Linear(self.knowledge_n + self.low_dim, self.net1, bias=False)
        self.drop_1 = nn.Dropout(p=0.5)
        self.prednet_full2 = nn.Linear(self.knowledge_n + self.low_dim, self.net1, bias=False)
        self.drop_2 = nn.Dropout(p=0.5)
        self.prednet_full3 = nn.Linear(1 * self.net1, 1)
        self.layer1 = nn.Linear(self.low_dim, 1)

        # initialization
        for name, param in self.named_parameters():
            if 'weight' in name:
                nn.init.xavier_normal_(param)

        self.fc1 = nn.Linear(self.pp_dim + self.knowledge_n, self.knowledge_n)
        self.fc2 = nn.Linear(self.pp_dim + self.knowledge_n, self.knowledge_n)

    def forward(self, stu_id, exer_id, kn_emb):
        knowledge_low_emb = self.knowledge_emb(self.k_index)

        #-----------------------------------------------------
        prompt_k = self.prompt_k(exer_id)
        old_k = self.generalize_layer_k(prompt_k)
        old_k = torch.sigmoid(torch.mm(old_k, knowledge_low_emb.T))
        new_k = torch.cat([prompt_k,old_k],dim=1)
        batch_exer_emb = torch.sigmoid(self.fc1(new_k))
        #-----------------------------------------------------
        batch_exer_vector = batch_exer_emb.repeat(1, self.knowledge_n).reshape(batch_exer_emb.shape[0], self.knowledge_n, batch_exer_emb.shape[1])

        #------------------------------------------------------------------
        old_stu = self.student_emb(stu_id)
        old_stu = torch.sigmoid(torch.mm(old_stu, knowledge_low_emb.T))
        new_stu = self.transform_layer_stu(old_stu)
        batch_stu_emb = torch.sigmoid(self.fc2(new_stu))

        #------------------------------------------------------------------
        batch_stu_vector = batch_stu_emb.repeat(1, self.knowledge_n).reshape(batch_stu_emb.shape[0], self.knowledge_n,
                                                                             batch_stu_emb.shape[1])

        kn_vector = knowledge_low_emb.repeat(batch_stu_emb.shape[0], 1).reshape(batch_stu_emb.shape[0],
                                                                                knowledge_low_emb.shape[0],
                                                                                knowledge_low_emb.shape[1])

        preference = torch.sigmoid(self.prednet_full1(torch.cat((batch_stu_vector, kn_vector), dim=2)))
        diff = torch.sigmoid(self.prednet_full2(torch.cat((batch_exer_vector, kn_vector), dim=2)))
        o = torch.sigmoid(self.prednet_full3(preference - diff))

        sum_out = torch.sum(o * kn_emb.unsqueeze(2), dim=1)
        count_of_concept = torch.sum(kn_emb, dim=1).unsqueeze(1)
        output = sum_out / count_of_concept
        return output

class Net(nn.Module):
    def __init__(self, knowledge_n, exer_n, student_n, low_dim, pp_dim, s_ranges):
        self.knowledge_n = knowledge_n
        self.exer_n = exer_n
        self.stu_n = student_n
        self.low_dim = low_dim
        self.pp_dim = pp_dim
        self.s_ranges = s_ranges
        self.net1 = knowledge_n
        self.prednet_input_len = self.knowledge_n
        self.prednet_len1, self.prednet_len2 = 512, 256

        super(Net, self).__init__()

        self.student_emb = nn.Embedding(self.stu_n, self.low_dim)
        self.knowledge_emb = nn.Embedding(self.knowledge_n, self.low_dim)
        self.k_difficulty = nn.Embedding(self.exer_n, self.low_dim)

        self.k_index = torch.LongTensor(list(range(self.knowledge_n)))

        self.prednet_full1 = nn.Linear(self.knowledge_n + self.low_dim, self.net1, bias=False)
        self.drop_1 = nn.Dropout(p=0.5)
        self.prednet_full2 = nn.Linear(self.knowledge_n + self.low_dim, self.net1, bias=False)
        self.drop_2 = nn.Dropout(p=0.5)
        self.prednet_full3 = nn.Linear(1 * self.net1, 1)
        self.layer1 = nn.Linear(self.low_dim, 1)

        for name, param in self.named_parameters():
            if 'weight' in name:
                nn.init.xavier_normal_(param)

    def forward(self, stu_id, exer_id, kn_emb):

        knowledge_low_emb = self.knowledge_emb(self.k_index)
        # step2.
        batch_stu_emb = self.student_emb(stu_id)
        batch_stu_emb = torch.sigmoid(torch.mm(batch_stu_emb, knowledge_low_emb.T))
        batch_stu_vector = batch_stu_emb.repeat(1, self.knowledge_n).reshape(batch_stu_emb.shape[0], self.knowledge_n, batch_stu_emb.shape[1])

        batch_exer_emb = self.k_difficulty(exer_id)
        batch_exer_emb = torch.sigmoid(torch.mm(batch_exer_emb, knowledge_low_emb.T))

        batch_exer_vector = batch_exer_emb.repeat(1, self.knowledge_n).reshape(batch_exer_emb.shape[0], self.knowledge_n, batch_exer_emb.shape[1])

        kn_vector = knowledge_low_emb.repeat(batch_stu_emb.shape[0], 1).reshape(batch_stu_emb.shape[0],
                                                                                knowledge_low_emb.shape[0],
                                                                                knowledge_low_emb.shape[1])


        preference = torch.sigmoid(self.prednet_full1(torch.cat((batch_stu_vector, kn_vector), dim=2)))
        diff = torch.sigmoid(self.prednet_full2(torch.cat((batch_exer_vector, kn_vector), dim=2)))
        o = torch.sigmoid(self.prednet_full3(preference - diff))

        sum_out = torch.sum(o * kn_emb.unsqueeze(2), dim=1)
        count_of_concept = torch.sum(kn_emb, dim=1).unsqueeze(1)
        output = sum_out / count_of_concept
        return output

class KSCD:
    def __init__(self, knowledge_n, exer_n, s_stu_n, t_stu_n, low_dim, pp_dim, s_ranges, model_file, target_model_file):
        super(KSCD, self).__init__()
        self.pp_dim = pp_dim
        self.model_file = model_file
        self.target_model_file = target_model_file
        self.kscd_s_net = Source_Net(knowledge_n, exer_n, s_stu_n, low_dim, pp_dim, s_ranges)
        self.kscd_t_net = Target_Net(knowledge_n, exer_n, t_stu_n, low_dim, pp_dim, s_ranges)
        self.kscd_t_net2 = Target_Net2(knowledge_n, exer_n, t_stu_n, low_dim, pp_dim, s_ranges)

    def Source_train(self, train_data, test_data=None, max_epochs=50, early_stopping_patience=2, device="cpu",
                     lr=0.001, silence=False):
        self.kscd_s_net = self.kscd_s_net.to(device)
        self.kscd_s_net.k_index = self.kscd_s_net.k_index.to(device)
        self.kscd_s_net.train()
        loss_function = nn.BCELoss()
        optimizer = optim.Adam(self.kscd_s_net.parameters(), lr=lr)

        epoch = 0
        best_auc = 0.0
        consecutive_no_improvement = 0

        while max_epochs is None or epoch < max_epochs:
            epoch_losses = []
            batch_count = 0

            for batch_data in tqdm(train_data, "Epoch %s" % epoch):
                batch_count += 1
                user_id, item_id, knowledge_emb, y = batch_data
                user_id: torch.Tensor = user_id.to(device)
                item_id: torch.Tensor = item_id.to(device)
                knowledge_emb: torch.Tensor = knowledge_emb.to(device)
                y: torch.Tensor = y.to(device)

                pred: torch.Tensor = self.kscd_s_net(user_id, item_id, knowledge_emb)

                loss = loss_function(pred.squeeze(1), y)

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                epoch_losses.append(loss.mean().item())
            average_loss = float(np.mean(epoch_losses))
            print("[Epoch %d] average loss: %.6f" % (epoch, average_loss))

            if test_data is not None:
                auc, accuracy = self.Source_net_eval(self.kscd_s_net,test_data, device=device)
                print("[Epoch %d] auc: %.6f, accuracy: %.6f" % (epoch, auc, accuracy))

                e = auc - best_auc
                if e > 0.001:
                    best_auc = auc
                    consecutive_no_improvement = 0

                    torch.save(self.kscd_s_net.state_dict(), self.model_file)
                    print(f"Saved the best model with AUC: {best_auc} at epoch {epoch}")

                else:
                    if e > 0:
                        best_auc = auc
                    consecutive_no_improvement += 1

                if early_stopping_patience is not None and consecutive_no_improvement >= early_stopping_patience:
                    print(
                        f"Early stopping at epoch {epoch} as there is no improvement in {early_stopping_patience} consecutive epochs.")
                    break

            epoch += 1

        with open("record.txt", "a") as f:
            f.write(f"Best AUC: {best_auc}, Epoch: {epoch}\n")

    def Target_train(self, model, train_data, test_data=None, epoch=50, device="cpu", lr=0.001, silence=False, patience=10):
        kscd_t_net = model.to(device)
        kscd_t_net.train()
        kscd_t_net.k_index = kscd_t_net.k_index.to(device)

        loss_function = nn.BCELoss()
        optimizer = optim.Adam(kscd_t_net.parameters(), lr=lr)

        best_auc = 0.0
        best_metrics = None
        early_stop_counter = 0

        for epoch_i in range(epoch):
            epoch_losses = []
            batch_count = 0
            for batch_data in tqdm(train_data, "Epoch %s" % epoch_i):
                batch_count += 1
                user_id, item_id, knowledge_emb, y = batch_data
                user_id: torch.Tensor = user_id.to(device)
                item_id: torch.Tensor = item_id.to(device)
                knowledge_emb: torch.Tensor = knowledge_emb.to(device)
                y: torch.Tensor = y.to(device)
                pred: torch.Tensor = kscd_t_net(user_id, item_id, knowledge_emb)
                loss = loss_function(pred.squeeze(1), y)

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                epoch_losses.append(loss.mean().item())

            average_loss = np.mean(epoch_losses)
            print("[Epoch %d] average loss: %.6f" % (epoch_i, float(average_loss)))

            if test_data is not None:
                auc, accuracy, rmse, f1 = self.Target_net_eval(kscd_t_net, test_data, device=device)
                print("[Epoch %d] auc: %.6f, accuracy: %.6f, RMSE: %.6f, F1: %.6f" % (epoch_i, auc, accuracy, rmse, f1))

                e = auc - best_auc
                if e > 0.0001:
                    best_auc = auc
                    best_metrics = (auc, accuracy, rmse, f1)
                    early_stop_counter = 0
                    torch.save(kscd_t_net.state_dict(), self.target_model_file)
                    print(f"Saved the best target model with AUC: {best_auc} at epoch {epoch}")
                else:
                    if e > 0:
                        best_auc = auc
                        best_metrics = (auc, accuracy, rmse, f1)
                    early_stop_counter += 1

                if early_stop_counter >= patience:
                    print(f"Early stopping at epoch {epoch_i}. No improvement for {patience} epochs.")
                    break

    def Source_net_eval(self, model, test_data, device="cpu"):
        ncdm_s_net = model.to(device)
        ncdm_s_net.eval()
        y_true, y_pred = [], []
        for batch_data in tqdm(test_data, "Evaluating"):
            user_id, item_id, knowledge_emb, y = batch_data
            user_id: torch.Tensor = user_id.to(device)
            item_id: torch.Tensor = item_id.to(device)
            knowledge_emb: torch.Tensor = knowledge_emb.to(device)
            pred: torch.Tensor = ncdm_s_net(user_id, item_id, knowledge_emb)
            y_pred.extend(pred.detach().cpu().tolist())
            y_true.extend(y.tolist())

        return roc_auc_score(y_true, y_pred), accuracy_score(y_true, np.array(y_pred) >= 0.5)

    def Target_net_eval(self, model, test_data, device="cpu"):
        ncdm_t_net = model.to(device)
        ncdm_t_net.eval()
        y_true, y_pred = [], []
        for batch_data in tqdm(test_data, "Evaluating"):
            user_id, item_id, knowledge_emb, y = batch_data
            user_id: torch.Tensor = user_id.to(device)
            item_id: torch.Tensor = item_id.to(device)
            knowledge_emb: torch.Tensor = knowledge_emb.to(device)
            pred: torch.Tensor = ncdm_t_net(user_id, item_id, knowledge_emb)
            y_pred.extend(pred.detach().cpu().tolist())
            y_true.extend(y.tolist())

        rmse = np.sqrt(mean_squared_error(y_true, y_pred))

        y_pred_binary = np.array(y_pred) >= 0.5
        f1 = f1_score(y_true, y_pred_binary)

        auc = roc_auc_score(y_true, y_pred)
        accuracy = accuracy_score(y_true, y_pred_binary)

        return auc, accuracy, rmse, f1


    def Transfer_parameters(self, model, s_ranges):
        self.kscd_s_net.load_state_dict(torch.load(self.model_file))

        model.prompt_k.weight.data.copy_(
            self.kscd_s_net.prompt_k.data)

        model.fc1.weight.data = self.kscd_s_net.fc1.weight.clone()
        model.fc1.bias.data = self.kscd_s_net.fc1.bias.clone()
        model.fc2.weight.data = self.kscd_s_net.fc2.weight.clone()
        model.fc2.bias.data = self.kscd_s_net.fc2.bias.clone()

        model.prednet_full1.weight.data = self.kscd_s_net.prednet_full1.weight.data.clone()

        model.prednet_full2.weight.data = self.kscd_s_net.prednet_full2.weight.data.clone()

        model.prednet_full3.weight.data = self.kscd_s_net.prednet_full3.weight.data.clone()

        for i in range(len(s_ranges)):
            model.transform_layer_stu.s_exer_vectors[i].data.copy_(
                self.kscd_s_net.s_stu_vectors[i].data)
            model.transform_layer_stu.s_exer_vectors[i].requires_grad = True

    def Target_test(self, model, test_data, device="cpu"):
        t_irt_net = model.to(device)
        t_irt_net.eval()
        y_true, y_pred = [], []
        for batch_data in tqdm(test_data, "Evaluating"):
            user_id, item_id, y = batch_data
            user_id: torch.Tensor = user_id.to(device)
            item_id: torch.Tensor = item_id.to(device)
            pred: torch.Tensor = t_irt_net(user_id, item_id)
            y_pred.extend(pred.detach().cpu().tolist())
            y_true.extend(y.tolist())

        rmse = np.sqrt(mean_squared_error(y_true, y_pred))

        y_pred_binary = np.array(y_pred) >= 0.5
        f1 = f1_score(y_true, y_pred_binary)

        auc = roc_auc_score(y_true, y_pred)
        accuracy = accuracy_score(y_true, y_pred_binary)

        return auc, accuracy, rmse, f1

