import torch
from torch import nn
import numpy as np

from FrEIA.framework import InputNode, OutputNode, Node, ReversibleGraphNet
from FrEIA.modules import rev_multiplicative_layer, permute_layer

from loss import mse, mse_tv, mmd_multiscale_on

from scipy.interpolate import interp1d
import h5py

from copy import deepcopy
from itertools import accumulate
import pickle

import os
from sys import exit

PadOp = '!!PAD'
ZeroPadOp = '!!ZeroPadding'

def schema_min_len(schema, zeroPadding):
    length = sum(s[1] if s[0] != PadOp else 0 for s in schema) \
            + zeroPadding * (len([s for s in schema if s[0] != PadOp]) - 1)
    return length
        
class DataSchema1D:
    def __init__(self, inp, minLength, zeroPadding, zero_pad_fn=torch.zeros):
        self.zero_pad = zero_pad_fn

        # Check schema is valid
        padCount = sum(1 if i[0] == PadOp else 0 for i in inp)
        for i in range(len(inp)-1):
            if inp[i][0] == PadOp and inp[i+1][0] == PadOp:
                raise ValueError('Schema cannot contain two consecutive \'!!PAD\' instructions.')

        # if padCount > 1:
        #     raise ValueError('Schema can only contain one \'!!PAD\' instruction.')
        if len([i for i in inp if i[0] != PadOp]) > len(set([i[0] for i in inp if i[0] != PadOp])):
            raise ValueError('Schema names must be unique within a schema.')
        
        # Find length without extra padding (beyond normal channel separation)
        length = schema_min_len(inp, zeroPadding)
        if (minLength - length) // padCount != (minLength - length) / padCount:
            raise ValueError('Schema padding isn\'t divisible by number of PadOps')

        # Build schema
        schema = []
        padding = (ZeroPadOp, zeroPadding)
        for j, i in enumerate(inp):
            if i[0] == PadOp:
                if j == len(inp) - 1:
                    # Count the edge case where '!!PAD' is the last op and a spurious
                    # extra padding gets inserted before it
                    if schema[-1] == padding:
                        del schema[-1]

                if length < minLength:
                    schema.append((ZeroPadOp, (minLength - length) // padCount))
                continue

            schema.append(i)
            if j != len(inp) - 1:
                schema.append(padding)

        if padCount == 0 and length < minLength:
            schema.append((ZeroPadOp, minLength - length))
        
        # Fuse adjacent zero padding -- no rational way to have more than two in a row 
        fusedSchema = []
        i = 0
        while True:
            if i >= len(schema):
                break

            if i < len(schema) - 1  and schema[i][0] == ZeroPadOp and schema[i+1][0] == ZeroPadOp:
                fusedSchema.append((ZeroPadOp, schema[i][1] + schema[i+1][1]))
                i += 1
            else:
                fusedSchema.append(schema[i])
            i += 1

        # Also remove 0-width ZeroPadding
        fusedSchema = [s for s in fusedSchema if s != (ZeroPadOp, 0)]
        self.schema = fusedSchema
        schemaTags = [s[0] for s in self.schema if s[0] != ZeroPadOp]
        tagIndices = [0] + list(accumulate([s[1] for s in self.schema]))
        tagRange = [(s[0], range(tagIndices[i], tagIndices[i+1])) for i, s in enumerate(self.schema) if s[0] != ZeroPadOp]
        for name, r in tagRange:
            setattr(self, name, r)
        self.len = tagIndices[-1]

    def __len__(self):
        return self.len

    def fill(self, entries, zero_pad_fn=None, batchSize=None, checkBounds=False, dev='cpu'):
        # Try and infer batchSize
        if batchSize is None:
            for k, v in entries.items():
                if not callable(v):
                    batchSize = v.shape[0]
                    break
            else:
                raise ValueError('Unable to infer batchSize from entries (all fns?). Set batchSize manually.')
        
        if checkBounds:
            try:
                for s in self.schema:
                    if s[0] == ZeroPadOp:
                        continue
                    entry = entries[s[0]]
                    if not callable(entry):
                        if len(entry.shape) != 2:
                            raise ValueError('Entry: %s must be a 2D array or fn.' % s[0])
                        if entry.shape[0] != batchSize:
                            raise ValueError('Entry: %s does not match batchSize along dim=0.' % s[0]) 
                        if entry.shape[1] != s[1]:
                            raise ValueError('Entry: %s does not match schema dimension.' % s[0]) 
            except KeyError as e:
                raise ValueError('No key present in entries to schema: ' + repr(e))
         
        # Use different zero_pad if specified
        if zero_pad_fn is None:
             zero_pad_fn = self.zero_pad
        
        # Fill in the schema, throw exception if entry is missing
        reifiedSchema = []
        try:
            for s in self.schema:
                if s[0] == ZeroPadOp:
                    reifiedSchema.append(zero_pad_fn(batchSize, s[1]))
                else:
                    entry = entries[s[0]]
                    if callable(entry):
                        reifiedSchema.append(entry(batchSize, s[1]))
                    else:
                        if s[0] == 'mc' or s[0] == 'phi' or s[0] == 't0': 
                            entry = entry.reshape(entry.shape[0],1)
                        reifiedSchema.append(entry)
        except KeyError as e:
            raise ValueError('No key present in entries to schema: ' + repr(e))

        reifiedSchema = torch.cat(reifiedSchema, dim=1)
        return reifiedSchema

    def __repr__(self):
        return repr(self.schema)

class F_fully_connected_leaky(nn.Module):
    '''Fully connected tranformation, not reversible, but used below.'''

    def __init__(self, size_in, size, internal_size=None, dropout=0.0,
                 batch_norm=False, leaky_slope=0.01):
        super(F_fully_connected_leaky, self).__init__()
        if not internal_size:
            internal_size = 2*size

        self.d1 = nn.Dropout(p=dropout)
        self.d2 = nn.Dropout(p=dropout)
        self.d2b = nn.Dropout(p=dropout)

        self.fc1 = nn.Linear(size_in, internal_size)

        self.fc2 = nn.Linear(internal_size, internal_size)
        self.fc2b = nn.Linear(internal_size, internal_size)
        self.fc2c = nn.Linear(internal_size, internal_size)
        #self.fc2d  = nn.Linear(internal_size, internal_size)

        self.fc3 = nn.Linear(internal_size, size)

        self.nl1 = nn.LeakyReLU(negative_slope=leaky_slope)
        self.nl2 = nn.LeakyReLU(negative_slope=leaky_slope)
        self.nl2b = nn.LeakyReLU(negative_slope=leaky_slope)
        #self.nl1 = nn.ReLU()
        #self.nl2 = nn.ReLU()
        #self.nl2b = nn.ReLU()
        self.nl2c = nn.LeakyReLU(negative_slope=leaky_slope)
        #self.nl2d = nn.ReLU()

        if batch_norm:
            self.bn1 = nn.BatchNorm1d(internal_size)
            self.bn1.weight.data.fill_(1)
            self.bn2 = nn.BatchNorm1d(internal_size)
            self.bn2.weight.data.fill_(1)
            self.bn2b = nn.BatchNorm1d(internal_size)
            self.bn2b.weight.data.fill_(1)
        self.batch_norm = batch_norm

    # define forward process layers?
    def forward(self, x):
        out = self.fc1(x)
        if self.batch_norm:
            out = self.bn1(out)
        out = self.nl1(self.d1(out))

        out = self.fc2(out)
        if self.batch_norm:
            out = self.bn2(out)
        out = self.nl2(self.d2(out))

        out = self.fc2b(out)
        if self.batch_norm:
            out = self.bn2b(out)
        out = self.nl2b(self.d2b(out))

        out = self.fc2c(out)
        out = self.nl2c(out)

        #out = self.fc2d(out)
        #out = self.nl2d(out)

        out = self.fc3(out)
        return out

class RadynversionNet(ReversibleGraphNet):
    def __init__(self, inputs, outputs, zeroPadding=0, numInvLayers=5, dropout=0.00, minSize=None, clamp=2.0):
        # Determine dimensions and construct DataSchema
        inMinLength = schema_min_len(inputs, zeroPadding)
        outMinLength = schema_min_len(outputs, zeroPadding)
        minLength = max(inMinLength, outMinLength)
        if minSize is not None:
            minLength = max(minLength, minSize)
        self.inSchema = DataSchema1D(inputs, minLength, zeroPadding)
        self.outSchema = DataSchema1D(outputs, minLength, zeroPadding)

        # check is both input and output shape of network is same
        if len(self.inSchema) != len(self.outSchema):
            raise ValueError('Input and output schemas do not have the same dimension.')

        # Build net graph
        inp = InputNode(len(self.inSchema), name='Input (0-pad extra channels)')
        nodes = [inp]

        # add requested number of nodes to INN
        for i in range(numInvLayers):
            nodes.append(Node([nodes[-1].out0], rev_multiplicative_layer,
                         {'F_class': F_fully_connected_leaky, 'clamp': clamp,
                          'F_args': {'dropout': dropout}}, name='Inv%d' % i))
            if (i != numInvLayers - 1):
                nodes.append(Node([nodes[-1].out0], permute_layer, {'seed': i}, name='Permute%d' % i))

        nodes.append(OutputNode([nodes[-1].out0], name='Output'))
        # Build net
        super().__init__(nodes)


class RadynversionTrainer:
    def __init__(self, model, atmosData, dev):
        self.model = model
        self.atmosData = atmosData
        self.dev = dev
        self.mmFns = None

        for mod_list in model.children():
            for block in mod_list.children():
                for coeff in block.children():
                    coeff.fc3.weight.data = 1e-3*torch.randn(coeff.fc3.weight.shape)
#                    coeff.fc3.weight.data = 1e-2*torch.randn(coeff.fc3.weight.shape)

        self.model.to(dev)

    def training_params(self, numEpochs, lr=2e-3, miniBatchesPerEpoch=20, metaEpoch=12, miniBatchSize=None, 
                        l2Reg=2e-5, wMSEf=1500, wMSEr=1500, wLatent=300, wRev=500, zerosNoiseScale=5e-3, fadeIn=True,
                        loss_fit=mse, loss_latent=None, loss_backward=None):
        if miniBatchSize is None:
            miniBatchSize = self.atmosData.batchSize

        if loss_latent is None:
            loss_latent = mmd_multiscale_on(self.dev)

        if loss_backward is None:
            loss_backward = mmd_multiscale_on(self.dev)

        decayEpochs = (numEpochs * miniBatchesPerEpoch) // metaEpoch
        gamma = 0.004**(1.0 / decayEpochs)

        # self.optim = torch.optim.Adam(self.model.parameters(), lr=lr, betas=(0.8, 0.8),
        #                               eps=1e-06, weight_decay=l2Reg)
        self.optim = torch.optim.Adam(self.model.parameters(), lr=lr, betas=(0.8, 0.8),
                                      eps=1e-06, weight_decay=l2Reg)
        self.scheduler = torch.optim.lr_scheduler.StepLR(self.optim,
                                                         step_size=metaEpoch,
                                                         gamma=gamma)
        self.wMSEf = wMSEf
        self.wMSEr = wMSEr
        self.fadeIn = fadeIn
        self.wLatent = wLatent
        self.wRev = wRev
        self.zerosNoiseScale = zerosNoiseScale
        self.miniBatchSize = miniBatchSize
        self.miniBatchesPerEpoch = miniBatchesPerEpoch
        self.numEpochs = numEpochs
        self.loss_fit = loss_fit
        self.loss_latent = loss_latent
        self.loss_backward = loss_backward

    def train(self, epoch):
        self.model.train()

        lTot = 0
        miniBatchIdx = 0

        # define scale of padding
        if self.fadeIn:
            wRevScale = min(epoch / (0.4 * self.numEpochs), 1)**3
            print(wRevScale)
        else:
            wRevScale = 1.0
        # dfine scale of padding
        noiseScale = (1.0 - wRevScale) * self.zerosNoiseScale
        # noiseScale = self.zerosNoiseScale

        pad_fn = lambda *x: noiseScale * torch.randn(*x, device=self.dev) #+ 10 * torch.ones(*x, device=self.dev)
#         zeros = lambda *x: torch.zeros(*x, device=self.dev)
        # get a random gaussian
        randn = lambda *x: torch.randn(*x, device=self.dev)
        losses = [0, 0, 0, 0]

        for x, y in self.atmosData.trainLoader:
            miniBatchIdx += 1

            if miniBatchIdx > self.miniBatchesPerEpoch:
                break

            # define pars and time series vectors
            x, y = x.to(self.dev), y.to(self.dev)
            yClean = y.clone()

            # fill parameter vector
            xp = self.model.inSchema.fill({'mc': x[:, 0], 
                                           'phi': x[:, 1],
                                           't0': x[:, 2]},
                                          zero_pad_fn=pad_fn)
            # fill time series and latent space vector
            yzp = self.model.outSchema.fill({'timeseries': y[:], 
                                             'LatentSpace': randn},
                                            zero_pad_fn=pad_fn)

            self.optim.zero_grad()

            # compute time series prediction given parameters
            out = self.model(xp)

            # compute L2 mean-squared forward loss. Input is time series and latent space variables.
            # output is time series / latent space prediction of model.
 
            # if uncommented, include z as well.
            #lForward = self.wMSEf * self.loss_fit(yzp[:, self.model.outSchema.LatentSpace[0]:],#[-1]+1:], 
            #                                      out[:, self.model.outSchema.LatentSpace[0]:])#[-1]+1:])
            # if uncommented, don't include z in MSE forward loss
            lForward = self.wMSEf * self.loss_fit(yzp[:, self.model.outSchema.LatentSpace[-1]+1:], 
                                                  out[:, self.model.outSchema.LatentSpace[-1]+1:])
            # store forward loss after having normalized by the predefined forward weighting
            losses[0] += lForward.data.item() / self.wMSEf

            # concatenate time series prediction and latent space prediction
            outLatentGradOnly = torch.cat((out[:, self.model.outSchema.timeseries].data, 
                                           out[:, self.model.outSchema.LatentSpace]), 
                                          dim=1)
            # concatenate true time series and latent space.
            unpaddedTarget = torch.cat((yzp[:, self.model.outSchema.timeseries], 
                                        yzp[:, self.model.outSchema.LatentSpace]), 
                                       dim=1)

            #outLatentGradOnly_y = out[:, self.model.outSchema.timeseries].data
            #unpaddedTarget_y = yzp[:, self.model.outSchema.timeseries]

            #outLatentGradOnly_z = out[:, self.model.outSchema.LatentSpace]
            #unpaddedTarget_z = yzp[:, self.model.outSchema.LatentSpace]
            
            # calculate MMD forward loss 
            lForward2 = self.wLatent * self.loss_latent(outLatentGradOnly, unpaddedTarget)
            #lForward2_y = self.wLatent * self.loss_latent(outLatentGradOnly_y, unpaddedTarget_y)
            #lForward2_z = self.wLatent * self.loss_latent(outLatentGradOnly_z, unpaddedTarget_z)

            # store loss after having normalized by latent weighting
            losses[1] += lForward2.data.item() / self.wLatent
            # add MMD loss to L2 loss to get total forward loss
            lForward += lForward2

            # store loss after having normalized by latent weighting
            #losses[1] += (lForward2_y / self.wLatent) + (lForward2_z / self.wLatent)
            # add MMD loss to L2 loss to get total forward loss
            #lForward += lForward2_y + lForward2_z
            
            
            # add total forward loss total loss
            lTot += lForward.data.item()

            # TODO: what is goig on here?
            lForward.backward()

            # get predicted latent space and true time series
            yzpRev = self.model.outSchema.fill({'timeseries': yClean[:], 
                                                'LatentSpace': out[:, self.model.outSchema.LatentSpace].data},
                                               zero_pad_fn=pad_fn)
            # get random gaussian latent space and true time series
            yzpRevRand = self.model.outSchema.fill({'timeseries': yClean[:], 
                                                    'LatentSpace': randn},
                                                   zero_pad_fn=pad_fn)

            # get predicted pars given predicted laten space and true time series
            outRev = self.model(yzpRev, rev=True)
            # get predicted pars given rand gaussian latent and true time series
            outRevRand = self.model(yzpRevRand, rev=True)

            # THis guy should have been OUTREVRAND!!!
            # xBack = torch.cat((outRevRand[:, self.model.inSchema.ne],
            #                    outRevRand[:, self.model.inSchema.temperature],
            #                    outRevRand[:, self.model.inSchema.vel]),
            #                   dim=1)
            # lBackward = self.wRev * wRevScale * self.loss_backward(xBack, x.reshape(self.miniBatchSize, -1))

            # calculate reverse loss using random latent variables / true time series predicted pars
            # compare to true parameters
            lBackward = self.wRev * wRevScale * self.loss_backward(outRevRand[:, self.model.inSchema.mc[0]:self.model.inSchema.t0[-1]+1], 
                                                                   xp[:, self.model.inSchema.mc[0]:self.model.inSchema.t0[-1]+1])

            scale = wRevScale if wRevScale != 0 else 1.0
            # store backward loss and normalize by reverse weighting
            losses[2] += lBackward.data.item() / (self.wRev * scale)

            #lBackward2 += 0.5 * self.wPred * self.loss_fit(outRev, xp)
            # calculate backward MMD loss using predicted pars given latent space and true time series.
            # compare to true parameters. Why is 0.5 hard coded here?
            lBackward2 = self.wMSEr * self.loss_fit(outRev[:, self.model.inSchema.mc[0]:self.model.inSchema.t0[-1]+1], 
                                                               xp[:, self.model.inSchema.mc[0]:self.model.inSchema.t0[-1]+1])

            # store backward loss normalized by predicted weight (also a factor of two in here for some reason?)
            losses[3] += lBackward2.data.item() / self.wMSEr
            lBackward += lBackward2
            
            # add backward loss to total loss value
            lTot += lBackward.data.item()

            # TODO: What is this?
            lBackward.backward()

            # make gradient be between -15.0 and 15.0
            for p in self.model.parameters():
                p.grad.data.clamp_(-15.0, 15.0)

            self.optim.step()

        # append losses
        losses = [l / miniBatchIdx for l in losses]
        return lTot / miniBatchIdx, losses

    def test(self, maxBatches=10):
        self.model.eval()

        forwardError = []
        backwardError = []

        batchIdx = 0
        
        if maxBatches == -1:
            maxBatches = len(self.atmosData.testLoader)

        pad_fn = lambda *x: torch.zeros(*x, device=self.dev) # 10 * torch.ones(*x, device=self.dev)
        randn = lambda *x: torch.randn(*x, device=self.dev)
        with torch.no_grad():
            for x, y in self.atmosData.testLoader:
                batchIdx += 1
                if batchIdx > maxBatches:
                    break

                x, y = x.to(self.dev), y.to(self.dev)

                inp = self.model.inSchema.fill({'mc': x[:, 0],
                                                'phi': x[:, 1],
                                                't0': x[:, 2]},
                                               zero_pad_fn=pad_fn)
                inpBack = self.model.outSchema.fill({'timeseries': y[:],
                                                     'LatentSpace': randn},
                                                    zero_pad_fn=pad_fn)
                                                    
                out = self.model(inp)
                f = self.loss_fit(out[:, self.model.outSchema.timeseries], y[:])
                forwardError.append(f)

                outBack = self.model(inpBack, rev=True)
#                 b = self.loss_fit(out[:, self.model.inSchema.ne], x[:, 0]) + \
#                     self.loss_fit(out[:, self.model.inSchema.temperature], x[:, 1]) + \
#                     self.loss_fit(out[:, self.model.inSchema.vel], x[:, 2])
                b = self.loss_backward(outBack, inp)
                backwardError.append(b)
        
            fE = torch.mean(torch.tensor(forwardError))
            bE = torch.mean(torch.tensor(backwardError))

            return fE, bE, out, outBack
        
    def review_mmd(self):
        with torch.no_grad():
            # Latent MMD
            loadIter = iter(self.atmosData.testLoader)
            # This is fine and doesn't load the first batch in testLoader every time, as shuffle=True
            x1, y1 = next(loadIter)
            x1, y1 = x1.to(self.dev), y1.to(self.dev)
            pad_fn = lambda *x: torch.zeros(*x, device=self.dev) # 10 * torch.ones(*x, device=self.dev)
            randn = lambda *x: torch.randn(*x, device=self.dev)
            xp = self.model.inSchema.fill({'mc': x1[:, 0],
                                           'phi': x1[:, 1],
                                           't0': x1[:, 2]},
                                          zero_pad_fn=pad_fn)
            yp = self.model.outSchema.fill({'timeseries': y1[:], 
                                           'LatentSpace': randn},
                                          zero_pad_fn=pad_fn)
            yFor = self.model(xp)
            yForNp = torch.cat((yFor[:, self.model.outSchema.timeseries], yFor[:, self.model.outSchema.LatentSpace]), dim=1).to(self.dev)
            ynp = torch.cat((yp[:, self.model.outSchema.timeseries], yp[:, self.model.outSchema.LatentSpace]), dim=1).to(self.dev)

            # Backward MMD
            xBack = self.model(yp, rev=True)

            # define total range of alphas to sweep over during training. Alpha determines kernel size used in MMD loss calculation.
            # by default min is 0.5 and max is 500
            r = np.logspace(np.log10(0.5), np.log10(10), num=2000)
            mmdValsFor = []
            mmdValsBack = []
            if self.mmFns is None:
                self.mmFns = []
                for a in r:
                    mm = mmd_multiscale_on(self.dev, alphas=[float(a)])
                    self.mmFns.append(mm)

            for mm in self.mmFns:
                mmdValsFor.append(mm(yForNp, ynp).item())
                mmdValsBack.append(mm(xp[:, self.model.inSchema.mc[0]:self.model.inSchema.t0[-1]+1], xBack[:, self.model.inSchema.mc[0]:self.model.inSchema.t0[-1]+1]).item())


            def find_new_mmd_idx(a):
                aRev = a[::-1]
                for i, v in enumerate(a[-2::-1]):
                    if v < aRev[i]:
                        return min(len(a)-i, len(a)-1)
            mmdValsFor = np.array(mmdValsFor)
            mmdValsBack = np.array(mmdValsBack)
            idxFor = find_new_mmd_idx(mmdValsFor)
            idxBack = find_new_mmd_idx(mmdValsBack)
#             idxFor = np.searchsorted(r, 2.0) if idxFor is None else idxFor
#             idxBack = np.searchsorted(r, 2.0) if idxBack is None else idxBack
            idxFor = idxFor if not idxFor is None else np.searchsorted(r, 2.0)
            idxBack = idxBack if not idxBack is None else np.searchsorted(r, 2.0)

            self.loss_backward = mmd_multiscale_on(self.dev, alphas=[float(r[idxBack])])
            self.loss_latent = mmd_multiscale_on(self.dev, alphas=[float(r[idxFor])])

            return r, mmdValsFor, mmdValsBack, idxFor, idxBack


class AtmosData:
    def __init__(self, dataLocations, test_split, ref_gps_time, resampleWl=None, logscale=False, normscale=False):
        if type(dataLocations) is str:
            dataLocations = [dataLocations]

        data={'pos': [], 'labels': [], 'x': [], 'sig': []}
        for filename in os.listdir(dataLocations[0]):
            if logscale: 
                data_temp={'pos': np.log10(h5py.File(dataLocations[0]+filename, 'r')['pos'][:]),
                  'labels': h5py.File(dataLocations[0]+filename, 'r')['labels'][:],
                  'x': h5py.File(dataLocations[0]+filename, 'r')['x'][:],
                  'sig': h5py.File(dataLocations[0]+filename, 'r')['sig'][:]}
            else: 
                data_temp={'pos': h5py.File(dataLocations[0]+filename, 'r')['pos'][:],
                  'labels': h5py.File(dataLocations[0]+filename, 'r')['labels'][:],
                  'x': h5py.File(dataLocations[0]+filename, 'r')['x'][:],
                  'sig': h5py.File(dataLocations[0]+filename, 'r')['sig'][:]}

            data['pos'].append(data_temp['pos'])
            data['labels'].append(data_temp['labels'])
            data['x'].append(data_temp['x'])
            data['sig'].append(data_temp['sig'])

        data['pos'] = np.concatenate(np.array(data['pos']), axis=0)
        data['labels'] = np.concatenate(np.array(data['labels']), axis=0)
        data['x'] = np.concatenate(np.array(data['x']), axis=0)
        data['sig'] = np.concatenate(np.array(data['sig']), axis=0)

        #TODO: may need to not log the training data
        self.pos = data['pos'][:]
        self.labels = data['labels'][:]
        self.sig = data['sig'][:]
        self.pos_test = data['pos'][-test_split:]
        self.labels_test = data['labels'][-test_split:]
        self.sig_test = data['sig'][-test_split:]
        self.x = data['x']

        # convert gps time to be diff between ref_time and actual time.
        #data['pos'][:,3] = ref_gps_time - data['pos'][:,3]

        data['pos']=data['pos'][:-test_split]
        data['labels']=data['labels'][:-test_split]
        self.mc = torch.tensor(data['pos'][:,0]).float()#.log10_()
        #self.lum_dist = torch.tensor(data['pos'][:,1]).float()#.log10_()
        self.phi = torch.tensor(data['pos'][:,1]).float()#.log10()
        self.t0 = torch.tensor(data['pos'][:,2]).float()
        self.timeseries = torch.tensor(data['labels'][:]).float()#.log10()
        self.atmosIn=data['pos'][:]
        self.atmosOut=data['labels'][:]

        if normscale:
            self.mc = torch.tensor(data['pos'][:,0]).float()/np.max(data['pos'][:,0])#.log10_()
            #self.lum_dist = torch.tensor(data['pos'][:,1]).float()/np.max(data['pos'][:,1])#.log10_()
            self.phi = torch.tensor(data['pos'][:,1]).float()/np.max(data['pos'][:,1])#.log10()
            self.t0 = torch.tensor(data['pos'][:,2]).float()/np.max(data['pos'][:,2])

            normscales = [np.max(data['pos'][:,0]),np.max(data['pos'][:,1]),np.max(data['pos'][:,2])]#,np.max(data['pos'][:,3])]
            data['pos'][:,0]=data['pos'][:,0]/normscales[0]
            data['pos'][:,1]=data['pos'][:,1]/normscales[1]
            data['pos'][:,2]=data['pos'][:,2]/normscales[2]
            #data['pos'][:,3]=data['pos'][:,3]/normscales[3]

            self.atmosIn=data['pos']#[:]
            self.normscales=normscales
        else:
            normscales=[]
            self.normscales=normscales

    def split_data_and_init_loaders(self, batchSize, splitSeed=41, padLines=False, linePadValue='Edge', zeroPadding=0, testingFraction=0.2):
        self.batchSize = batchSize

        #if padLines and linePadValue == 'Edge':
        #    lPad0Size = (self.ne.shape[1] - self.lines[0].shape[1]) // 2
        #    rPad0Size = self.ne.shape[1] - self.lines[0].shape[1] - lPad0Size
        #    lPad1Size = (self.ne.shape[1] - self.lines[1].shape[1]) // 2
        #    rPad1Size = self.ne.shape[1] - self.lines[1].shape[1] - lPad1Size
        #    if any(np.array([lPad0Size, rPad0Size, lPad1Size, rPad1Size]) <= 0):
        #        raise ValueError('Cannot pad lines as they are already bigger than/same size as the profiles!')
        #    lPad0 = torch.ones(self.lines[0].shape[0], lPad0Size) * self.lines[0][:, 0].unsqueeze(1)
        #    rPad0 = torch.ones(self.lines[0].shape[0], rPad0Size) * self.lines[0][:, -1].unsqueeze(1)
        #    lPad1 = torch.ones(self.lines[1].shape[0], lPad1Size) * self.lines[1][:, 0].unsqueeze(1)
        #    rPad1 = torch.ones(self.lines[1].shape[0], rPad1Size) * self.lines[1][:, -1].unsqueeze(1)

        #    self.lineOut = torch.stack([torch.cat((lPad0, self.lines[0], rPad0), dim=1), torch.cat((lPad1, self.lines[1], rPad1), dim=1)]).permute(1, 0, 2)
        #elif padLines:
        #    lPad0Size = (self.ne.shape[1] - self.lines[0].shape[1]) // 2
        #    rPad0Size = self.ne.shape[1] - self.lines[0].shape[1] - lPad0Size
        #    lPad1Size = (self.ne.shape[1] - self.lines[1].shape[1]) // 2
        #    rPad1Size = self.ne.shape[1] - self.lines[1].shape[1] - lPad1Size
        #    if any(np.array([lPad0Size, rPad0Size, lPad1Size, rPad1Size]) <= 0):
        #        raise ValueError('Cannot pad lines as they are already bigger than/same size as the profiles!')
        #    lPad0 = torch.ones(self.lines[0].shape[0], lPad0Size) * linePadValue
        #    rPad0 = torch.ones(self.lines[0].shape[0], rPad0Size) * linePadValue
        #    lPad1 = torch.ones(self.lines[1].shape[0], lPad1Size) * linePadValue
        #    rPad1 = torch.ones(self.lines[1].shape[0], rPad1Size) * linePadValue

        #    self.lineOut = torch.stack([torch.cat((lPad0, self.lines[0], rPad0), dim=1), torch.cat((lPad1, self.lines[1], rPad1), dim=1)]).permute(1, 0, 2)
        #else:
        #    self.lineOut = torch.stack([self.lines[0], self.lines[1]]).permute(1, 0, 2)

        #indices = np.arange(self.atmosIn.shape[0])
        #np.random.RandomState(seed=splitSeed).shuffle(indices)

        # split off 20% for testing
        #maxIdx = int(self.atmosIn.shape[0] * (1.0 - testingFraction)) + 1
        #if zeroPadding != 0:
        #    trainIn = torch.cat((self.atmosIn[indices][:maxIdx], torch.zeros(maxIdx, self.atmosIn.shape[1], zeroPadding)), dim=2)
        #    trainOut = torch.cat((self.lineOut[indices][:maxIdx], torch.zeros(maxIdx, self.lineOut.shape[1], zeroPadding)), dim=2)
        #    testIn = torch.cat((self.atmosIn[indices][maxIdx:], torch.zeros(self.atmosIn.shape[0] - maxIdx, self.atmosIn.shape[1], zeroPadding)), dim=2)
        #    testOut = torch.cat((self.lineOut[indices][maxIdx:], torch.zeros(self.atmosIn.shape[0] - maxIdx, self.lineOut.shape[1], zeroPadding)), dim=2)
        #else:
        test_num = int(self.atmosIn.shape[0]*testingFraction)

        trainIn = self.atmosIn[:-test_num]
        trainOut = self.atmosOut[:-test_num]
        testIn = self.atmosIn[-test_num:]
        testOut = self.atmosOut[-test_num:]

        self.testLoader = torch.utils.data.DataLoader(
                    torch.utils.data.TensorDataset(torch.tensor(testIn), torch.tensor(testOut)), 
                    batch_size=batchSize, shuffle=True, drop_last=True)
        self.trainLoader = torch.utils.data.DataLoader(
                    torch.utils.data.TensorDataset(torch.tensor(trainIn), torch.tensor(trainOut)),
                    batch_size=self.batchSize, shuffle=True, drop_last=True)
                    


