import torch
from torch import nn, optim
import torch.nn.functional as F
from torchvision import models
import numpy as np
import matplotlib.pyplot as plt
from torch.distributions import Normal

from resnet_nyu_dataloader import setup_data_loaders
from torch.utils.tensorboard import SummaryWriter


torch.cuda.set_device(1)

# Deep Residual Learning for Image Recognition: https://arxiv.org/pdf/1512.03385.pdf
class ResBlock(nn.Module):
  def __init__(self, in_channels, out_channels):
    super().__init__()

    self.conv = nn.Sequential(nn.Conv2d(in_channels, out_channels, 3, padding=1), nn.BatchNorm2d(out_channels), nn.ReLU(),
                              nn.Conv2d(out_channels, out_channels, 3, padding=1), nn.BatchNorm2d(out_channels))

    self.linear = None
    if in_channels != out_channels:
      self.linear = nn.Sequential(nn.Conv2d(in_channels, out_channels, 1, padding=0), nn.BatchNorm2d(out_channels))

  def forward(self, x):
    residual = self.conv(x)
    if self.linear:
      x = self.linear(x)
    return F.relu(residual + x)


# Input is 128*128
class Encoder(nn.Module):
  def __init__(self, z_dim):
    super().__init__()

    self.resnet = models.resnet34(pretrained=True)

    freeze = ['conv1', 'layer1', 'layer2', 'fc']
    for name, child in self.resnet.named_children():
      if child in freeze:
        for param in child.parameters():
          param.requires_grad = False

    self.resnet.conv1 = nn.Conv2d(4, 64, kernel_size=(7, 7), stride=(2, 2), padding=(3, 3), bias=False)
    self.resnet.fc = nn.Linear(in_features=512, out_features=z_dim*2, bias=True)

  def forward(self, x):
    final = self.resnet(x)
    mean, std = torch.chunk(final, 2, dim=1)
    std = F.softplus(std) + 1e-5
    return Normal(loc=mean, scale=std)


class Decoder(nn.Module):
  def __init__(self, z_dim, std=1):
    super().__init__()

    filters = 32
    depths = [filters*16, filters*16, filters*8, filters*4, filters*2, filters, 4]

    self.fc = nn.Linear(z_dim, depths[0]*4**2)
    self.std = std

    convs = []
    for i in range(0, len(depths)-2):
        convs.append(nn.Sequential(ResBlock(depths[i], depths[i]), nn.ConvTranspose2d(depths[i], depths[i+1], 4, padding=1, stride=2), nn.BatchNorm2d(depths[i+1]), nn.LeakyReLU(0.2)))

    convs.append(nn.Sequential(ResBlock(depths[-2], depths[-2]), nn.ConvTranspose2d(depths[-2], depths[-1], 4, padding=1, stride=2)))
    self.convs = nn.Sequential(*convs)

  def forward(self, z):
    fc_out = self.fc(z).view(z.shape[0], -1, 4, 4)
    conv_out = self.convs(fc_out)
    final = torch.tanh(conv_out)

    return Normal(loc=final, scale=torch.ones_like(final) * self.std)

class VAE(nn.Module):
  def __init__(self, z_dim):
    super().__init__()
    self.z_dim = z_dim
    self.encoder = Encoder(self.z_dim)
    self.decoder = Decoder(self.z_dim)

    if torch.cuda.is_available():
      self.cuda()

  def forward(self, x):
    latent = self.encoder(x)
    z = latent.rsample()
    return self.decoder(z), x, latent

  def reconstruct(self, x):
      latent = self.encoder(x)
      z = latent.rsample()
      return self.decoder(z).mean


def neg_elbo(reconstructed, x, latent):
    log_likelihood_reconstructed = reconstructed.log_prob(x).mean(dim=0).sum()

    # -KL for gaussian case: 0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
    # kl = kl_divergence(latent, Normal(torch.zeros_like(latent.loc), torch.ones_like(latent.scale))).sum(1).mean()
    kl = torch.mean(-0.5 * torch.sum(1 + torch.log(latent.variance) - latent.mean.pow(2) - latent.variance, dim=1))

    elbo = log_likelihood_reconstructed - kl
    return -elbo

# Trains for one epoch
def train(vae, train_loader, optimizer):
    vae.train()
    epoch_loss = 0
    for x in train_loader:
      if torch.cuda.is_available():
        x = x.cuda()

      optimizer.zero_grad()
      output = vae(x)
      loss = neg_elbo(*output)
      epoch_loss += loss.item()

      loss.backward()
      optimizer.step()

    # return epoch loss
    total_epoch_loss_train = epoch_loss / len(train_loader)
    return total_epoch_loss_train


def mse(vae, test_loader):
    vae.eval()
    mse = 0
    with torch.no_grad():
      # compute the loss over the entire test set
      for x in test_loader:
        if torch.cuda.is_available():
          x = x.cuda()

        mean = vae.reconstruct(x)
        mse += F.mse_loss(mean, x).item()

    mse = mse / len(test_loader)
    return mse


writer = SummaryWriter(log_dir='/gruvi/usr/shimi/logs/resnet')

vae = VAE(400)
model_params = [param for param in vae.parameters() if param.requires_grad]
optimizer = optim.Adam(model_params, lr=1e-4)

NUM_EPOCHS = 500
TEST_FREQUENCY = 5
BATCH_SIZE = 50
train_loader, test_loader = setup_data_loaders(batch_size=BATCH_SIZE, normalize=True)

train_elbo = []
test_elbo = []

best = float('inf')

fig, axs = plt.subplots(2, 2)

for epoch in range(1, NUM_EPOCHS+1):
    total_epoch_loss_train = train(vae, train_loader, optimizer)
    writer.add_scalar('Loss/train', -total_epoch_loss_train, epoch)
    writer.add_scalar('Loss/mse', mse(vae, test_loader), epoch)

    print("[epoch %d]  average training loss: %.8f" % (epoch, total_epoch_loss_train))

    if epoch % TEST_FREQUENCY == 0:
        # total_epoch_loss_test = evaluate(vae, test_loader)
        # writer.add_scalar('Loss/test', -total_epoch_loss_test, epoch)
        # print("[epoch %d] average test loss: %.8f" % (epoch, total_epoch_loss_test))

        # Save stuff
        # if total_epoch_loss_test < best:
        #   print('SAVING EPOCH', epoch)
        #   best = total_epoch_loss_test
          # torch.save({
          #   'epoch': epoch,
          #   'model_state_dict': vae.state_dict(),
          #   'optimizer_state_dict': optimizer.state_dict(),
          #   }, 'rgbd_checkpoint.save')

        # if total_epoch_loss_test < 0:  # numerical instability occured
        #   print('Negative loss occurred!!!', total_epoch_loss_test)
        #   break

        for i in range(0, 100, 10):
            axs[0, 0].imshow(test_loader.dataset[i][:3].permute(1, 2, 0))
            axs[0, 1].imshow(test_loader.dataset[i][3])
            test_input = test_loader.dataset[i].unsqueeze(0).cuda()
            reconstructed = vae.reconstruct(test_input).cpu().detach()[0]
            axs[1, 0].imshow(reconstructed[:3].permute(1, 2, 0))
            axs[1, 1].imshow(reconstructed[3])
            writer.add_figure('reconstruction{}'.format(i), fig, epoch)
            plt.cla()