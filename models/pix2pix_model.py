import torch
from .base_model import BaseModel
from . import networks
from util.util import SSIM
from util.util import visualize_tensor , label2color


class Pix2PixModel(BaseModel):
    """ This class implements the pix2pix model, for learning a mapping from input images to output images given paired data.

    The model training requires '--dataset_mode aligned' dataset.
    By default, it uses a '--netG unet256' U-Net generator,
    a '--netD basic' discriminator (PatchGAN),
    and a '--gan_mode' vanilla GAN loss (the cross-entropy objective used in the orignal GAN paper).

    pix2pix paper: https://arxiv.org/pdf/1611.07004.pdf
    """
    @staticmethod
    def modify_commandline_options(parser, is_train=True):
        """Add new dataset-specific options, and rewrite default values for existing options.

        Parameters:
            parser          -- original option parser
            is_train (bool) -- whether training phase or test phase. You can use this flag to add training-specific or test-specific options.

        Returns:
            the modified parser.

        For pix2pix, we do not use image buffer
        The training objective is: GAN Loss + lambda_L1 * ||G(A)-B||_1
        By default, we use vanilla GAN loss, UNet with batchnorm, and aligned datasets.
        """
        # changing the default values to match the pix2pix paper (https://phillipi.github.io/pix2pix/)
        parser.set_defaults(norm='batch', netG='unet_256', dataset_mode='aligned')
        if is_train:
            parser.set_defaults(pool_size=0, gan_mode='vanilla')
            parser.add_argument('--lambda_L1', type=float, default=100.0, help='weight for L1 loss')

        return parser

    def __init__(self, opt):
        """Initialize the pix2pix class.

        Parameters:
            opt (Option class)-- stores all the experiment flags; needs to be a subclass of BaseOptions
        """
        BaseModel.__init__(self, opt)
        self.opt = opt
        # specify the training losses you want to print out. The training/test scripts will call <BaseModel.get_current_losses>
        self.loss_names = ['G_GAN', 'G_L1', 'D_real', 'D_fake']
        self.extra_val_loss_names = ['ssim']
        # specify the images you want to save/display. The training/test scripts will call <BaseModel.get_current_visuals>
        self.visual_names = ['fake_B', 'real_B', 'range', 'real_A',
                             'proj_label', 'proj_mask', 'points', 'sem_label', 'proj_idx']
        # specify the models you want to save to the disk. The training/test scripts will call <BaseModel.save_networks> and <BaseModel.load_networks>
        if self.isTrain:
            self.model_names = ['G', 'D']
        else:  # during test time, only load G
            self.model_names = ['G']
        # define networks (both generator and discriminator)
        self.netG = networks.define_G(opt.input_nc, opt.output_nc, opt.ngf, opt.netG, opt.norm,
                                      not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids)

        self.netD = networks.define_D(opt.input_nc + opt.output_nc, opt.ndf, opt.netD,
                                        opt.n_layers_D, opt.norm, opt.init_type, opt.init_gain, self.gpu_ids)

        # define loss functions
        self.criterionGAN = networks.GANLoss(opt.gan_mode).to(self.device)
        self.criterionL1 = torch.nn.L1Loss(reduction='sum')
        self.crterionSSIM = SSIM()
        # initialize optimizers; schedulers will be automatically created by function <BaseModel.setup>.
        if self.isTrain:
            self.optimizer_G = torch.optim.Adam(self.netG.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizer_D = torch.optim.Adam(self.netD.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizers.append(self.optimizer_G)
            self.optimizers.append(self.optimizer_D)

    def set_input(self, input):
        """Unpack input data from the dataloader and perform necessary pre-processing steps.

        Parameters:
            input (dict): include the data itself and its metadata information.

        The option 'direction' can be used to swap images in domain A and domain B.
        """
        AtoB = self.opt.direction == 'AtoB'
        self.real_A = input['A' if AtoB else 'B'].to(self.device)
        self.real_B = input['B' if AtoB else 'A'].to(self.device)
        self.image_paths = input['A_paths' if AtoB else 'B_paths']

    def set_input_PCL(self, data):
        proj_xyz, proj_range, proj_remission, proj_mask, proj_rgb, proj_label, proj_idx, points, sem_label = data
        if self.opt.input_nc == 3:
            self.real_A = proj_xyz.to(self.device)
        elif self.opt.input_nc == 6:
            self.real_A = torch.cat([proj_xyz, proj_rgb], dim=1).to(self.device)
        elif self.opt.input_nc == 4:
            self.real_A = torch.cat([proj_xyz, proj_label], dim=1).to(self.device)
        elif self.opt.input_nc == 7:
            self.real_A = torch.cat([proj_xyz, proj_rgb, proj_label], dim=1).to(self.device)
        self.real_B = proj_remission.to(self.device)
        self.proj_mask = proj_mask.to(self.device)
        self.range = proj_range
        self.proj_label = proj_label if len(proj_label) != 0 else torch.zeros_like(proj_range)
        self.points = points
        self.sem_label = sem_label
        self.proj_idx = proj_idx
        # visualize_tensor(label2color(proj_label[0]))
        # visualize_tensor(proj_range[0])
        # visualize_tensor(proj_remission[0])
               
    def evaluate_model(self):
        self.forward()
        self.calc_loss_D()
        self.calc_loss_G(is_eval=True)

    def forward(self):
        """Run forward pass; called by both functions <optimize_parameters> and <test>."""
        self.fake_B = self.netG(self.real_A) * self.proj_mask  # G(A)
        

    def calc_loss_D(self):
        """Calculate GAN loss for the discriminator"""
        # Fake; stop backprop to the generator by detaching fake_B
        fake_AB = torch.cat((self.real_A, self.fake_B), 1)  # we use conditional GANs; we need to feed both input and output to the discriminator
        pred_fake = self.netD(fake_AB.detach())
        self.loss_D_fake = self.criterionGAN(pred_fake, False)
        # Real
        real_AB = torch.cat((self.real_A, self.real_B), 1)
        pred_real = self.netD(real_AB)
        self.loss_D_real = self.criterionGAN(pred_real, True)
        # combine loss and calculate gradients
        self.loss_D = (self.loss_D_fake + self.loss_D_real) * 0.5

    def calc_loss_G(self, is_eval=True):
        """Calculate GAN and L1 loss for the generator"""
        # First, G(A) should fake the discriminator
        fake_AB = torch.cat((self.real_A, self.fake_B), 1)
        pred_fake = self.netD(fake_AB)
        self.loss_G_GAN = self.criterionGAN(pred_fake, True)
        # Second, G(A) = B
        self.loss_G_L1 = self.criterionL1(self.fake_B, self.real_B) / self.proj_mask.sum()
        if is_eval:
            self.loss_ssim = self.crterionSSIM(self.real_B, self.fake_B, self.proj_mask)
        # combine loss and calculate gradients
        self.loss_G = self.loss_G_GAN * self.opt.lambda_LGAN + self.loss_G_L1 * self.opt.lambda_L1
        

    def optimize_parameters(self):
        self.forward()                   # compute fake images: G(A)
        # update D
        self.set_requires_grad(self.netD, True)  # enable backprop for D
        self.optimizer_D.zero_grad()     # set D's gradients to zero
        self.calc_loss_D()
        self.loss_D.backward()                # calculate gradients for D
        self.optimizer_D.step()          # update D's weights
        # update G
        self.set_requires_grad(self.netD, False)  # D requires no gradients when optimizing G
        self.optimizer_G.zero_grad()        # set G's gradients to zero
        self.calc_loss_G()
        self.loss_G.backward()                   # calculate graidents for G
        self.optimizer_G.step()             # udpate G's weights
