import json
import numpy as np
import h5py
import cv2
import os
import torch
import random
from dataset.commons import trajs2featmap, drawtrajs

# OpenBLAS screws up with CPU affinity
# Spawned process will inherit this
os.sched_setaffinity(0,range(os.cpu_count()))

def find_border(img, threshold=10/255):
    # img: C H W
    #import matplotlib.pyplot as plt

    I = np.mean(img, 0) # H W
    silhouette_w = np.mean(I,0)
    silhouette_h = np.mean(I,1)
    
    h_len = silhouette_h.size
    w_len = silhouette_w.size
    
    
    for h_left in range(h_len):
        if silhouette_h[h_left] > threshold:
            break
    for h_right in range(h_len-1,-1,-1):
        if silhouette_h[h_right] > threshold:
            break
    for w_left in range(w_len):
        if silhouette_w[w_left] > threshold:
            break
    for w_right in range(w_len-1,-1,-1):
        if silhouette_w[w_right] > threshold:
            break
    
    # further remove some border
    h_left = min(h_left+1,h_len-1)
    h_right = max(h_right-1,0)
    w_left = min(w_left+1,w_len-1)
    w_right = max(w_right-1,0)

#    img[0:h_left,:,:] = 255
#    img[h_right+1:,:,:] = 255
#    img[:,0:w_left,:] = 255
#    img[:,w_right+1:,:] = 255

    mask = np.ones([1,h_len,w_len], dtype=np.float32)    
    mask[:,0:h_left,:] = 0
    mask[:,h_right+1:,:] = 0
    mask[:,:,0:w_left] = 0
    mask[:,:,w_right+1:] = 0
    
    #plt.imshow(mask[0,:,:])
    #plt.show()
    
    return mask
    #return (h_left, h_right, w_left, w_right)


    
class RPReader_traj:
    h5_handles = {}
    
    # max_interval=1 ==> use two frames
    def __init__(self, is_test=True, max_interval=10, min_ntraj=2, max_ntraj=6, is_eval=False):
        print('Initializing RPReader_traj...')
        self.is_test = is_test or is_eval
        self.is_eval = is_eval
        self.max_interval = max_interval
        self.min_ntraj = min_ntraj
        self.max_ntraj = max_ntraj
        with open(os.path.join(os.path.dirname(__file__), 'dataset_path.json'), 'r') as f:
            dataset_paths = json.load(f)
        if self.is_test:
            self.robot_push_traj_h5_path = dataset_paths['robot_push_traj_h5_test']
            self.robot_push_jpgs_h5_path = dataset_paths['robot_push_jpgs_h5_test']
            self.jpgs_h5_prefix = 'push/push_testnovel'
        else:
            self.robot_push_traj_h5_path = dataset_paths['robot_push_traj_h5_train']
            self.robot_push_jpgs_h5_path = dataset_paths['robot_push_jpgs_h5_train']
            self.jpgs_h5_prefix = 'push/push_train'
        print(' -- Dataset jpg H5 path: {}'.format(self.robot_push_jpgs_h5_path))
        print(' -- Trajectory H5 path: {}'.format(self.robot_push_traj_h5_path))
        print(' -- Mode: {}'.format('EVAL' if self.is_eval else ('Test' if self.is_test else 'Train')))
        print(' -- Maximum frame interval: {}'.format(self.max_interval))
        h5f = h5py.File(self.robot_push_traj_h5_path, 'r', libver='latest')
        traj_db = h5f["/RPTraj/by_clip"]
        print(' -- Trajectory count: {}'.format(len(traj_db)))
        # Init index translation table
        self.annots_table = []
        self.idxtrans_table = []
        self.interval_lut = []
        for i in range(max_interval):
            self.interval_lut.append([])
        max_available_interval = 0
        for clip_name in traj_db:
            annot_t = {}
            annot = traj_db[clip_name]
            annot_traj_len = annot.attrs['TrajLen']
            annot_clip_start = annot.attrs['StartFrame']
            num_trajs = annot.attrs['TrajCount']
            trajs = annot[()]
            vid_id = annot.attrs['VidId']
            annot_t['TrajLen'] = annot_traj_len
            annot_t['StartFrame'] = annot_clip_start
            annot_t['TrajCount'] = num_trajs
            annot_t['Trajs'] = trajs
            annot_t['VidId'] = vid_id
            max_available_interval = max(max_available_interval, annot_traj_len)
            self.annots_table.append(annot_t)
            if not self.is_eval: # if in EVAL mode, sample test data in __getitem__ function on-the-fly
                for interval in range(1, self.max_interval+1): # Note that interval starts from 1 !!! 
                    for offset in range(annot_traj_len-interval):
                        entry = (len(self.annots_table)-1, offset, interval)
                        self.idxtrans_table.append(entry)
                        self.interval_lut[interval-1].append(entry)
            else:
                self.idxtrans_table.append(len(self.annots_table)-1)
        print(' -- Sample count: {}'.format(len(self.idxtrans_table)))
        print(' -- Interval LUT entry count: {}'.format(len(self.interval_lut)))
        h5f.close()
        if max_available_interval < max_interval+1:
            print(' -- WARNING! Max trajectory duration less than max_interval+1, {} vs {}'.format(max_available_interval,max_interval+1))
        print(' -- Init done!')
    
    def __len__(self):
        return len(self.idxtrans_table)

        
    def _getitem_pair(self, idx):
        h_target = 192
        w_target = 240
        annot_id, offset, interval = self.idxtrans_table[idx]
        annot = self.annots_table[annot_id]
        vid_id = annot['VidId']
        vid_start_frame = annot['StartFrame']+offset
        trajs = annot['Trajs']
        #trajs = trajs[:,offset:offset+interval+1,:] # N duration, XY
        trajs = trajs[:,(offset,offset+interval),:] # N duration, XY
        num_trajs = trajs.shape[0]
        num_appear_trajs = min(num_trajs,random.randint(self.min_ntraj,self.max_ntraj))
        appear_trajs = random.sample(range(num_trajs), num_appear_trajs)
        trajs = trajs[appear_trajs,:,:]
        #print(trajs.shape)
        
        vid_seq = np.empty([2,3,h_target,w_target], dtype=np.float32)
        for frame_no in range(2):
            frame = cv2.imdecode(self.jpg_h5['{}/{}.jpg'.format(vid_id, vid_start_frame+interval*frame_no)][()], -1)
            if frame is None:
                print('Frame read failed:\n   {}'.format(img_path))
                return
            frame = frame.astype(np.float32) / 255.0    # Convert to range [0,1]
            frame = cv2.resize(frame, (w_target,h_target), interpolation=cv2.INTER_AREA)  # W H      
            #frame = drawtrajs(trajs, frame_no, frame)
            #cv2.imshow('image',frame)
            #cv2.waitKey(1000)
            vid_seq[frame_no,:,:,:] = np.transpose(frame, (2,0,1))[(2,1,0),:,:] # HWC to CHW, BGR to RGB
        vid_mask = find_border(vid_seq[1,:,:,:], threshold=10/255)
            
        # Build kpmap
        kpmap_seq = np.zeros([6,h_target,w_target], dtype=np.float32)
        kpmap_seq = trajs2featmap(trajs, kpmap_seq)

        img_input = vid_seq[0,:,:,:]
        warp_input = np.concatenate((vid_seq[0,:,:,:],kpmap_seq[:,:,:]),axis=0)
        img_gt = vid_seq[1,:,:,:]
        
        return img_input, warp_input, img_gt, vid_mask
        
    def _getitem_seq(self, idx):
        h_target = 192
        w_target = 240
        annot_id = self.idxtrans_table[idx]
        annot = self.annots_table[annot_id]
        interval = self.max_interval # 10 frames
        vid_len = annot['TrajLen']
        offset = random.randint(0, vid_len-self.max_interval)
        vid_id = annot['VidId']
        vid_start_frame = annot['StartFrame']+offset
        trajs = annot['Trajs']
        trajs = trajs[:,offset:offset+interval,:] # N duration, XY
        num_trajs = trajs.shape[0]
        num_appear_trajs = min(num_trajs,random.randint(self.min_ntraj,self.max_ntraj))
        appear_trajs = random.sample(range(num_trajs), num_appear_trajs)
        trajs = trajs[appear_trajs,:,:]
        #print(trajs.shape)
        
        vid_frame_stor = []
        vid_mask_stor = []
        for frame_no in range(interval):
            #vid_seq = np.empty([3,h_target,w_target], dtype=np.float32)
            
            frame = cv2.imdecode(self.jpg_h5['{}/{}.jpg'.format(vid_id, vid_start_frame+frame_no)][()], -1)
            if frame is None:
                print('Frame read failed:\n   {}'.format(img_path))
                return
            frame = frame.astype(np.float32) / 255.0    # Convert to range [0,1]
            frame = cv2.resize(frame, (w_target,h_target), interpolation=cv2.INTER_AREA)  # W H      
            
            #cv2.imshow('image',frame)
            #cv2.waitKey(100)
            #vid_seq[:,:,:] = np.transpose(frame, (2,0,1))[(2,1,0),:,:] # HWC to CHW, BGR to RGB
            frame = np.transpose(frame, (2,0,1))[(2,1,0),:,:] # HWC to CHW, BGR to RGB
            vid_frame_stor.append(frame)
            vid_mask = find_border(frame, threshold=10/255)
            vid_mask_stor.append(vid_mask)
            
        # Build kpmap
        warp_input_list = []
        img_input_list = []
        img_gt_list = []
        vid_mask_list = []
        for frame_no in range(1, interval):
            kpmap_seq = np.zeros([6,h_target,w_target], dtype=np.float32)
            trajs_sub = trajs[:,(0,frame_no),:]
            kpmap_seq = trajs2featmap(trajs_sub, kpmap_seq)
           
            warp_input = np.concatenate((vid_frame_stor[0],kpmap_seq[:,:,:]),axis=0)
            img_input = vid_frame_stor[0]
            img_gt = vid_frame_stor[frame_no]
            vid_mask = vid_mask_stor[frame_no]
            
            warp_input_list.append(warp_input)
            img_input_list.append(img_input)
            img_gt_list.append(img_gt)
            vid_mask_list.append(vid_mask)
            
            
        warp_input = np.stack(warp_input_list,axis=0) # [interval-1, 9, H, W]
        img_input = np.stack(img_input_list,axis=0) # [interval-1, 3, H, W]
        img_gt = np.stack(img_gt_list,axis=0) # [interval-1, 3, H, W]
        vid_mask = np.stack(vid_mask_list,axis=0)
        
        return img_input, warp_input, img_gt, vid_mask
        
        
        
    def __getitem__(self, idx):
        if not self.is_eval:
            return self._getitem_pair(idx)
        else:
            return self._getitem_seq(idx)
        
    def collate_fn(self, sample_list):
        z = zip(*sample_list)
        if self.is_eval:
            return [torch.cat([torch.from_numpy(b) for b in samples], 0) for samples in z]
        else:
            #batch = torch.stack([torch.from_numpy(b) for b in sample_list], 0)
            return [torch.stack([torch.from_numpy(b) for b in samples], 0) for samples in z]
        

    #def collate_fn(self, sample_list): # single batch passthrough
    #    return [torch.from_numpy(b) for b in sample_list[0]]
            
    def worker_init_fn(self, worker_id):
        print('Worker {} spawned. CPU affinity {}'.format(worker_id, os.sched_getaffinity(0)))
        self.worker_id = worker_id
        os.sched_setaffinity(0,range(os.cpu_count()))
        jpg_h5 = h5py.File(self.robot_push_jpgs_h5_path, 'r', libver='latest')
        self.jpg_h5 = jpg_h5[self.jpgs_h5_prefix]
        
        
        
import time            
def main():
    rp_reader = RPReader_traj()
    rp_reader.worker_init_fn(0)
    t_start = time.time()
    #for imgcnt in range(len(rp_reader)):
    for imgcnt in range(1000):
        rp_reader[imgcnt]
        #print(imgcnt)
    t_end = time.time()
    print(t_end - t_start)
    cv2.destroyAllWindows()
    
    

if __name__ == "__main__":
    main()




