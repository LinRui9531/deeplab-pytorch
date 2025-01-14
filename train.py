
import torch
import torch.nn as nn
import torchvision
import torchvision.transforms as transforms
import os
import sys
import numpy as np
import tqdm

from torch.autograd import Variable
from config import cfg
from lib.net.generateNet import generate_net
import torch.optim as optim
from PIL import Image
from tensorboardX import SummaryWriter
from torch.utils.data import DataLoader
from lib.net.loss import MaskLoss
from lib.net.sync_batchnorm.replicate import patch_replication_callback
from dataset import DataSet
from tensorboardX import SummaryWriter

esp = 1e-8
torch.backends.cudnn.benchmark = True

def train_net():
	train_cumtom_dataset = DataSet(pharse='train',cfg=cfg)
	train_dataloader = DataLoader(dataset=train_cumtom_dataset,
                            shuffle=True,
                            batch_size=cfg.TRAIN_BATCHES,
                            num_workers=cfg.DATA_WORKERS)

	val_cumtom_dataset = DataSet(pharse='val',cfg=cfg)
	val_dataloader = DataLoader(dataset=val_cumtom_dataset,
							shuffle=True,
							batch_size=cfg.TEST_BATCHES,
							num_workers=cfg.DATA_WORKERS)
	print('train dataset : {} ,with batch size :{}'.format(len(train_cumtom_dataset),cfg.TRAIN_BATCHES))
	print('train dataset : {} ,with batch size :{}'.format(len(val_cumtom_dataset), cfg.TEST_BATCHES))
	# dataset = generate_dataset(cfg.DATA_NAME, cfg, 'train', cfg.DATA_AUG)
	# dataloader = DataLoader(dataset,
	# 			batch_size=cfg.TRAIN_BATCHES,
	# 			shuffle=cfg.TRAIN_SHUFFLE,
	# 			num_workers=cfg.DATA_WORKERS,
	# 			drop_last=True)
	
	net = generate_net(cfg)
	

	print('Use %d GPU'%cfg.TRAIN_GPUS)
	device = torch.device(0)
	if cfg.TRAIN_GPUS > 1:
		net = nn.DataParallel(net)
		patch_replication_callback(net)
	net.to(device)		

	if cfg.TRAIN_CKPT:
		pretrained_dict = torch.load(cfg.TRAIN_CKPT)
		net_dict = net.state_dict()
		pretrained_dict = {k: v for k, v in pretrained_dict.items() if (k in net_dict) and (v.shape==net_dict[k].shape)}
		net_dict.update(pretrained_dict)
		net.load_state_dict(net_dict)
		# net.load_state_dict(torch.load(cfg.TRAIN_CKPT),False)
	
	criterion = MaskLoss()
	# optimizer = optim.SGD(
	# 	params = [
	# 		{'params': get_params(net.module,key='1x'), 'lr': cfg.TRAIN_LR},
	# 		{'params': get_params(net.module,key='10x'), 'lr': 10*cfg.TRAIN_LR}
	# 	],
	# 	momentum=cfg.TRAIN_MOMENTUM
	# )
	optimizer = optim.SGD(
		lr=cfg.TRAIN_LR,
		params = net.parameters(),
		momentum=cfg.TRAIN_MOMENTUM,
		weight_decay=cfg.TRAIN_WEIGHT_DECAY
	)
	#scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=cfg.TRAIN_LR_MST, gamma=cfg.TRAIN_LR_GAMMA, last_epoch=-1)
	# itr = cfg.TRAIN_MINEPOCH * len(dataloader)
	# max_itr = cfg.TRAIN_EPOCHS * len(dataloader)
	running_loss = 0.0

	tblogger = SummaryWriter()
	#net.eval()
	for epoch in range(cfg.TRAIN_MINEPOCH, cfg.TRAIN_EPOCHS):
		#scheduler.step()
		now_lr = adjust_lr(optimizer, epoch)

		avaliable_count = [esp for _ in range(cfg.MODEL_NUM_CLASSES)]
		avaliable_ious = [0.0 for _ in range(cfg.MODEL_NUM_CLASSES)]
		for i, data in enumerate(train_dataloader):
			img, mask = data
			img = Variable(img).float().cuda()
			mask = Variable(mask).float().cuda()
			#print(mask.shape, type(mask))
			optimizer.zero_grad()
			output = net(img)
			loss = criterion(output, mask)
			compute_iou(output,mask,avaliable_count,avaliable_ious)

			loss.backward()
			optimizer.step()

			running_loss += loss.item()
			if i!=0 and i % cfg.PRINT_FRE == 0:
				print('epoch:{}/{}\tbatch:{}/{}\tlr:{:.6f}\tloss:{:.6f}\tBsmoke:{:.6f}\tCorn:{:.6f}\tBrice:{:.6f}'.format(
					epoch, cfg.TRAIN_EPOCHS, i, len(train_dataloader),
					now_lr, running_loss / (i+1) , avaliable_ious[1]/avaliable_count[1], avaliable_ious[2]/avaliable_count[2]
					 , avaliable_ious[3] / avaliable_count[3]))

		tblogger.add_scalars('loss', {'train':running_loss / len(train_dataloader)}, epoch)
		tblogger.add_scalars('Bsmoke', {'train':avaliable_ious[1]/avaliable_count[1]}, epoch)
		tblogger.add_scalars('Corn', {'train':avaliable_ious[2]/avaliable_count[2]}, epoch)
		tblogger.add_scalars('Brice', {'train':avaliable_ious[3]/avaliable_count[3]}, epoch)

		running_loss = 0.0
			
		if epoch != 0 and epoch % cfg.SAVE_FRE == 0:
			save_path = os.path.join(cfg.MODEL_SAVE_DIR,'%s_%s_%s_epoch%d.pth'%(cfg.MODEL_NAME,cfg.MODEL_BACKBONE,cfg.DATA_NAME,epoch))
			torch.save(net.state_dict(), save_path)
			print('%s has been saved'%save_path)

		print('evalution at epoch {}'.format(epoch))
		eval(net,val_dataloader,criterion,tblogger,epoch)
		
	save_path = os.path.join(cfg.MODEL_SAVE_DIR,'%s_%s_%s_epoch%d_all.pth'%(cfg.MODEL_NAME,cfg.MODEL_BACKBONE,cfg.DATA_NAME,cfg.TRAIN_EPOCHS))		
	torch.save(net.state_dict(),save_path)
	if cfg.TRAIN_TBLOG:
		tblogger.close()
	print('%s has been saved'%save_path)
	print('train finished!')

def eval(net,dataloader,criterion,logger,epoch):
	net.eval()
	val_count = [esp for _ in range(cfg.MODEL_NUM_CLASSES)]
	val_ious = [0.0 for _ in range(cfg.MODEL_NUM_CLASSES)]
	val_loss = 0.0
	with torch.no_grad():
		for i ,data in tqdm.tqdm(enumerate(dataloader)):
			img, mask = data
			img = Variable(img).float().cuda()
			mask = Variable(mask).float().cuda()
			output = net(img)
			loss = criterion(output, mask)
			compute_iou(output,mask,val_count,val_ious)

			val_loss += loss.item()

		logger.add_scalars('loss', {'val':val_loss/len(dataloader)}, epoch)
		logger.add_scalars('Bsmoke', {'val':val_ious[1]/val_count[1]}, epoch)
		logger.add_scalars('Corn', {'val':val_ious[2]/val_count[2]}, epoch)
		logger.add_scalars('Brice', {'val':val_ious[3]/val_count[3]}, epoch)
	print('loss:{:.6f}\tBsmoke:{:.6f}\tCorn:{:.6f}\tBrice:{:.6f}'.format(
		val_loss/len(dataloader), val_ious[1] / val_count[1], val_ious[2] / val_count[2]
		, val_ious[3] / val_count[3]))

def compute_iou(output,target,counts,ious,num = cfg.MODEL_NUM_CLASSES):
	# 0 is background
	output = torch.sigmoid(output)#.cpu().numpy()
	output[output>=0.5] = 1
	output[output < 0.5] = 0
	n = output.shape[0]
	for i in range(1,num):#class

		target_i = target[:, i].detach().cpu().numpy()
		out_put_i = output[:, i].detach().cpu().numpy()
		insections = target_i * out_put_i
		unions = target_i +  out_put_i - insections

		out_put_i = out_put_i.sum(axis = (1,2))
		target_i = target_i.sum(axis = (1,2))
		insections = insections.sum(axis = (1,2))
		unions = unions.sum(axis = (1,2))
		# print(out_put_i,target_i,insections,unions,sep='\n')
		for j in range(n):
			if target_i[j] == 0:continue
			else:
				counts[i] = counts[i] + 1
				ious[i] += insections[j]/unions[j]


def adjust_lr(optimizer, epoch, max_epoch = cfg.TRAIN_EPOCHS):
	now_lr = cfg.TRAIN_LR * (1 - epoch/(max_epoch+1)) ** cfg.TRAIN_POWER
	optimizer.param_groups[0]['lr'] = now_lr
	# optimizer.param_groups[1]['lr'] = now_lr * 10
	return now_lr


def get_params(model, key):
	for m in model.named_modules():
		if key == '1x':
			if 'backbone' in m[0] and isinstance(m[1], nn.Conv2d):
				for p in m[1].parameters():
					yield p
		elif key == '10x':
			if 'backbone' not in m[0] and isinstance(m[1], nn.Conv2d):
				for p in m[1].parameters():
					yield p
if __name__ == '__main__':
	train_net()


