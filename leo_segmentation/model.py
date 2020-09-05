# Architecture definition
# Computational graph creation
import torch, os, numpy as np
from torch import nn
from torch.distributions import Normal
from torch.nn import CrossEntropyLoss
from torch.utils.tensorboard import SummaryWriter
from torchvision import models
from torch.nn import functional as F
from utils import display_data_shape, get_named_dict, calc_iou_per_class,\
    log_data, load_config, summary_write_masks
    
class Flatten(nn.Module):
  def __init__(self):
    super(Flatten, self).__init__()

  def forward(self, x):
    return torch.reshape(x, (x.shape[0], -1))

class Reshape(nn.Module):
  def __init__(self, dims):
    super(Reshape, self).__init__()
    self.dims = dims

  def forward(self, x):
    return x.view(self.dims)

class EncoderBlock(nn.Module):
    """
    Encoder with pretrained backbone
    """
    def __init__(self):
        super(EncoderBlock, self).__init__()
        self.layers = nn.ModuleList(list(models.vgg16_bn(pretrained=True).features))
    
    def forward(self,x):
        features = []
        output_layers = [4, 11, 21, 31, 41]
        for i, layer in enumerate(self.layers):
            x = layer(x)
            if i in output_layers:
                features.append(x)
        return x, features

def decoder_block(config, in_channels, out_channels, kernel_size, padding, dropout=True):
    conv_trans = nn.ConvTranspose2d(in_channels, out_channels, kernel_size, stride=2)
    layers = [nn.Conv2d(out_channels, out_channels, kernel_size, stride=1, padding=padding),
              nn.BatchNorm2d(out_channels),
              nn.ReLU(),
             ]
    if dropout:
        layers.extend(nn.Dropout(config.dropout_rate))
    conv_block = nn.Sequential(*layers)
    return conv_trans, conv_block
 
class DecoderBlock(nn.Module):
    """
    Leo Decoder
    """
    def __init__(self, config):
        super(DecoderBlock, self).__init__()
        self.conv_trans1, self.conv_1 = decoder_block(config, 8, 8, 3, 1)
        self.conv_trans2, self.conv_2 = decoder_block(config, 8, 8, 3, 1)
        self.conv_trans3, self.conv_3 = decoder_block(config, 8, 8, 3, 1)
        self.conv_trans4, self.conv_4 = decoder_block(config, 8, 8, 3, 1)
        self.conv_trans5, self.conv_5 = decoder_block(config, 8, 8, 3, 1)
        
    def forward(self, x, concat_features):
        o = self.conv_trans1(x)
        o = torch.cat([o, concat_features[-1]], dim=1)
        o = self.conv_1(o)
        o = self.conv_trans2(x)
        o = torch.cat([o, concat_features[-2]], dim=1)
        o = self.conv_2(o)
        o = self.conv_trans3(x)
        o = torch.cat([o, concat_features[-3]], dim=1)
        o = self.conv_3(o)
        o = self.conv_trans4(x)
        o = torch.cat([o, concat_features[-4]], dim=1)
        o = self.conv_4(o)
        o = self.conv_trans5(x)
        o = torch.cat([o, concat_features[-5]], dim=1)
        o = self.conv_5(o)
        return o

class LEO(nn.Module):
    """
    contains functions to perform latent embedding optimization
    """
    def __init__(self, config, mode="meta_train"):
        super(LEO, self).__init__()
        self.config = config
        self.mode = mode
        self.latent_size = config.hyperparameters.num_latents
        self.dense_input_shape = (28, 192, 256)
        self.encoder = self.EncoderBlock()
        self.decoder = self.DecoderBlock(config.hyperparameters)
        seg_network =  nn.Conv2d(30, 2, kernel_size=3, stride=1, padding=0)
        self.seg_weight = seg_network.weight
        self.seg_bias = seg_network.bias
        self.device  = torch.device("cuda:0" if torch.cuda.is_available() and config.use_gpu else "cpu")
        self.loss_fn = CrossEntropyLoss()

    def forward_encoder(self, x):
        return self.encoder(x)

    def forward_decoder(self, x, latents, weight, bias):
        o = self.decoder(latents)
        o = torch.cat([o, x], dim=1)
        o = F.conv2d(x, weight, bias)
        return o

    def calculate_inner_loss(self, x, weight, bias, target, latents=None):
        latents = latents if latents else self.forward_encoder(x)
        pred = self.forward_decoder(x, latents, weight, bias)
        loss = self.loss_fn(pred, target.long())
        return loss, pred, latents
        
    def leo_inner_loop(self, x, weight, bias, target):
        """
        This function does "latent code optimization" that is back propagation  until latent codes and
        updating the latent weights
        Args:
            data (dict) : contains tr_imgs, tr_masks, val_imgs, val_masks
            latents (tensor) : shape ((num_classes * num_eg_per_class), latent_channels, H, W)
        Returns:
            tr_loss : computed as crossentropyloss (groundtruth--> tr_imgs_mask, prediction--> einsum(tr_imgs, segmentation_weights))
            segmentation_weights : shape(num_classes, num_eg_per_class, channels, H, W)
        """
        inner_lr = self.config.hyperparameters.inner_loop_lr
        tr_loss, _ , latents = self.calculate_inner_loss(x, weight, bias, target)
        #initial_latents = latents.clone()   
        for _ in range(self.config.hyperparameters.num_adaptation_steps):
            latents_grad = torch.autograd.grad(tr_loss, [latents], create_graph=False)[0]
            with torch.no_grad():
                latents -= inner_lr * latents_grad
            tr_loss, _ , latents  = self.calculate_inner_loss(x, weight, bias, target, latents)
        return tr_loss 

    def finetuning_inner_loop(self, data, tr_loss, weight, bias):
        """
        This function does "segmentation_weights optimization"
        Args:
            data (dict) : contains tr_imgs, tr_masks, val_imgs, val_masks
            leo_loss (tensor_0shape) : computed as crossentropyloss (groundtruth--> tr_imgs_mask, prediction--> einsum(tr_imgs, segmentation_weights))
           segmentation_weights (tensor) : shape(num_classes, num_eg_per_class, channels, H, W)
        Returns:
            val_loss (tensor_0shape) : computed as crossentropyloss (groundtruth--> val_imgs_mask, prediction--> einsum(val_imgs, segmentation_weights))
        """
        finetuning_lr = self.config.hyperparameters.finetuning_lr
        grad_weight, grad_bias = torch.autograd.grad(tr_loss, [weight, bias])
        updated_weight = weight - finetuning_lr * grad_weight
        updated_bias = bias - finetuning_lr * grad_bias
        for _ in range(self.config.hyperparameters.num_finetuning_steps - 1):
            grad_weight, grad_bias = torch.autograd.grad(tr_loss, [updated_weight, updated_bias])
            with torch.no_grad():
                updated_weight -= finetuning_lr * grad_weight
                updated_bias -= finetuning_lr * grad_bias
            tr_loss, _ , _  = self.calculate_inner_loss(data.tr_imgs, updated_weight, updated_bias, data.tr_masks)
        val_loss, _, _ = self.calculate_inner_loss(data.val_imgs, updated_weight, updated_bias, data.val_masks)
        return val_loss

    def forward(self, tr_imgs, tr_masks, val_imgs, val_masks):
        metadata = (tr_imgs, tr_masks, val_imgs, val_masks, "") 
        data_dict = get_named_dict(metadata, 0)
        latents, kl_loss = self.forward_encoder(data_dict.tr_imgs)
        inner_loss, _, _ = self.forward_decoder(data_dict.tr_imgs, latents, data_dict.tr_masks)
        return inner_loss + kl_loss

    def compute_loss(self, metadata, train_stats, mode="meta_train"):
        """
        Computes the  outer loop loss

        Args:
            model (object) : leo model
            meta_dataloader (object): Dataloader 
            train_stats: (object): train stats object
            config (dict): config
            mode (str): meta_train, meta_val or meta_test
        Returns:
            (tuple) total_val_loss (list), train_stats
        """
        num_tasks = len(metadata[0])
        if train_stats.episode % self.config.display_stats_interval == 1:
            display_data_shape(metadata)
        total_val_loss = []

        for batch in range(num_tasks):
            data_dict = get_named_dict(metadata, batch)
            tr_loss = self.leo_inner_loop(data_dict.tr_imgs, self.seg_weight, self.seg_bias, data_dict.tr_masks)
            val_loss = self.finetuning_inner_loop(data_dict, tr_loss, self.seg_weight, self.seg_bias)
            total_val_loss.append(val_loss)

        total_val_loss = sum(total_val_loss)/len(total_val_loss)
        stats_data = {
            "mode": mode,
            "kl_loss": 0,
            "total_val_loss":total_val_loss
        }
        train_stats.update_stats(**stats_data)
        return total_val_loss, train_stats

    def evaluate_val_imgs(self, metadata, classes, train_stats, writer):
        log_msg = ""
        num_tasks = len(metadata[0])
        for batch in range(num_tasks):
            data_dict = get_named_dict(metadata, batch)
            latents, _ = self.forward_encoder(data_dict.val_imgs)
            _, _,  predictions = self.forward_decoder(data_dict.val_imgs, latents, data_dict.val_masks)
            iou = calc_iou_per_class(predictions, data_dict.val_masks)
            batch_msg = f"\nClass: {classes[batch]}, Episode: {train_stats.episode}, Val IOU: {iou}"
            print(batch_msg[1:])
            log_msg += batch_msg
            grid_title = f"pred_{train_stats.episode}_class_{classes[batch]}"
            summary_write_masks(predictions, writer, grid_title)
            grid_title = f"ground_truths_{train_stats.episode}_class_{classes[batch]}"
            summary_write_masks(data_dict.val_masks, writer, grid_title, ground_truth=True)
        log_filename = os.path.join(os.path.dirname(__file__), "data", "models",\
                         f"experiment_{self.config.experiment.number}", "val_stats_log.txt")
        log_data(log_msg, log_filename)


def save_model(model, optimizer, config, stats):
    """
    Save the model while training based on check point interval
    
    if episode number is not -1 then a prompt to delete checkpoints occur if 
    checkpoints for that episode number exits.
    This only occurs if the prompt_deletion flag in the experiment dictionary
    is true else checkpoints that already exists are automatically deleted

    Args:
        model - trained model       
        optimizer - optimized weights
        config - global config
        stats - dictionary containing stats for the current episode
    
    Returns:
    """
    data_to_save = {
        'mode': stats.mode,
        'episode': stats.episode,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'kl_loss': stats.kl_loss,
        'total_val_loss': stats.total_val_loss
    }

    experiment = config.experiment
    model_root = os.path.join(config.data_path, "models")
    model_dir = os.path.join(model_root, "experiment_{}" \
                             .format(experiment.number))

    checkpoint_path = os.path.join(model_dir, f"checkpoint_{stats.episode}.pth.tar")
    if not os.path.exists(checkpoint_path):
        torch.save(data_to_save, checkpoint_path)
    else:
        trials = 0
        while trials < 3:
            if experiment.prompt_deletion:
                print(f"Are you sure you want to delete checkpoint: {stats.episode}")
                print(f"Type Yes or y to confirm deletion else No or n")
                user_input = input()
            else:
                user_input = "Yes"
            positive_options = ["Yes", "y", "yes"]
            negative_options = ["No", "n", "no"]
            if user_input in positive_options:
                # delete checkpoint
                os.remove(checkpoint_path)
                torch.save(data_to_save, checkpoint_path)
                log_filename = os.path.join(model_dir, "model_log.txt")
                msg = msg = f"\n*********** checkpoint {stats.episode} was deleted **************"
                log_data(msg, log_filename)
                break

            elif user_input in negative_options:
                raise ValueError("Supply the correct episode number to start experiment")
            else:
                trials += 1
                print("Wrong Value Supplied")
                print(f"You have {3 - trials} left")
                if trials == 3:
                    raise ValueError("Supply the correct answer to the question")

def load_model(config):

    """
    Loads the model
    Args:
        config - global config
        **************************************************
        Note: The episode key in the experiment dict
        implies the checkpoint that should be loaded 
        when the model resumes training. If episode is 
        -1, then the latest model is loaded else it loads
        the checkpoint at the supplied episode
        *************************************************
    Returns:
        leo :loaded model that was saved
        optimizer: loaded weights of optimizer
        stats: stats for the last saved model
    """
    experiment = config.experiment
    model_dir  = os.path.join(config.data_path, "models", "experiment_{}"\
                 .format(experiment.number))
    
    checkpoints = os.listdir(model_dir)
    checkpoints = [i for i in checkpoints if os.path.splitext(i)[-1] == ".tar"]
    max_cp = max([int(cp.split(".")[0].split("_")[1]) for cp in checkpoints])
    #if experiment.episode == -1, load latest checkpoint
    episode = max_cp if experiment.episode == -1 else experiment.episode
    checkpoint_path = os.path.join(model_dir, f"checkpoint_{episode}.pth.tar")
    checkpoint = torch.load(checkpoint_path)

    log_filename = os.path.join(model_dir, "model_log.txt")
    msg =  f"\n*********** checkpoint {episode} was loaded **************" 
    log_data(msg, log_filename)
    
    leo = LEO(config)
    optimizer = torch.optim.Adam(leo.parameters(), lr=config.hyperparameters.outer_loop_lr)
    leo.load_state_dict(checkpoint['model_state_dict'])
    optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    mode = checkpoint['mode']
    total_val_loss = checkpoint['total_val_loss']
    kl_loss = checkpoint['kl_loss']

    stats = {
        "mode": mode,
        "episode": episode,
        "kl_loss": kl_loss,
        "total_val_loss": total_val_loss
        }

    return leo, optimizer, stats

