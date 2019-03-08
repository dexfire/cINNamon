"""
Tutorial to demonstrate running parameter estimation on a reduced parameter
space for an injected signal.

This example estimates the masses using a uniform prior in both component masses
and distance using a uniform in comoving volume prior on luminosity distance
between luminosity distances of 100Mpc and 5Gpc, the cosmology is Planck15.
"""
from __future__ import division, print_function

import numpy as np
import bilby
from sys import exit
import matplotlib
matplotlib.use('agg')
import matplotlib.pyplot as plt
from scipy import integrate, interpolate
import scipy
import lalsimulation
import lal
import time
import h5py
#from pylal import antenna, cosmography

def whiten_data(data,duration,sample_rate,psd,flag='fd'):
    """ Takes an input timeseries and whitens it according to a psd
    Parameters
    ----------
    data:
        data to be whitened
    duration:
        length of time series in seconds
    sample_rate:
        sampling frequency of time series
    psd:
        power spectral density to be used
    flag:
        if 'td': then do in time domain. if not: then do in frequency domain
    Returns
    -------
    xf:
        whitened signal 
    """

    if flag=='td':
        # FT the input timeseries - window first
        win = tukey(duration*sample_rate,alpha=1.0/8.0)
        xf = np.fft.rfft(win*data)
    else:
        xf = data

    # deal with undefined PDS bins and normalise
    idx = np.argwhere(psd>0.0)
    invpsd = np.zeros(psd.size)
    invpsd[idx] = 1.0/psd[idx]
    xf *= np.sqrt(2.0*invpsd/sample_rate)

    # Detrend the data: no DC component.
    xf[0] = 0.0

    if flag=='td':
        # Return to time domain.
        x = np.fft.irfft(xf)
        return x
    else:
        return xf

def tukey(M,alpha=0.5):
    """ Tukey window code copied from scipy.
    Parameters
    ----------
    M:
        Number of points in the output window.
    alpha:
        The fraction of the window inside the cosine tapered region.
    Returns
    -------
    w:
        The window
    """
    n = np.arange(0, M)
    width = int(np.floor(alpha*(M-1)/2.0))
    n1 = n[0:width+1]
    n2 = n[width+1:M-width-1]
    n3 = n[M-width-1:]

    w1 = 0.5 * (1 + np.cos(np.pi * (-1 + 2.0*n1/alpha/(M-1))))
    w2 = np.ones(n2.shape)
    w3 = 0.5 * (1 + np.cos(np.pi * (-2.0/alpha + 1 + 2.0*n3/alpha/(M-1))))
    w = np.concatenate((w1, w2, w3))

    return np.array(w[:M])

def make_bbh(hp,hc,fs,ra,dec,psi,det,ifos,event_time):
    """ Turns hplus and hcross into a detector output
    applies antenna response and
    and applies correct time delays to each detector
    Parameters
    ----------
    hp:
        h-plus version of GW waveform
    hc:
        h-cross version of GW waveform
    fs:
        sampling frequency
    ra:
        right ascension
    dec:
        declination
    psi:
        polarization angle        
    det:
        detector
    Returns
    -------
    ht:
        combined h-plus and h-cross version of waveform
    hp:
        h-plus version of GW waveform 
    hc:
        h-cross version of GW waveform
    """
    # compute antenna response and apply
    Fp=ifos.antenna_response(ra,dec,float(event_time),psi,'plus')
    Fc=ifos.antenna_response(ra,dec,float(event_time),psi,'cross')
    #Fp,Fc,_,_ = antenna.response(float(event_time), ra, dec, 0, psi, 'radians', det )
    ht = hp*Fp + hc*Fc     # overwrite the timeseries vector to reuse it

    return ht, hp, hc

def gen_template(duration,sampling_frequency,pars):
    # whiten signal

    # fix parameters here
    injection_parameters = dict(
        mass_1=pars['m1'], mass_2=pars['m2'], a_1=0.0, a_2=0.0, tilt_1=0.0, tilt_2=0.0,
        phi_12=0.0, phi_jl=0.0, luminosity_distance=pars['lum_dist'], theta_jn=pars['theta_jn'], psi=pars['psi'],
        phase=pars['phase'], geocent_time=pars['geocent_time'], ra=pars['ra'], dec=pars['dec'])

    # Fixed arguments passed into the source model
    waveform_arguments = dict(waveform_approximant='IMRPhenomPv2',
                              reference_frequency=50., minimum_frequency=20.)

    # Create the waveform_generator using a LAL BinaryBlackHole source function
    waveform_generator = bilby.gw.WaveformGenerator(
        duration=duration, sampling_frequency=sampling_frequency,
        frequency_domain_source_model=bilby.gw.source.lal_binary_black_hole,
        waveform_arguments=waveform_arguments)

    # create waveform
    wfg = waveform_generator
    wfg.parameters = injection_parameters
    freq_signal = wfg.frequency_domain_strain()
    time_signal = wfg.time_domain_strain()

    # Set up interferometers.  In this case we'll use two interferometers
    # (LIGO-Hanford (H1), LIGO-Livingston (L1). These default to their design
    # sensitivity
    ifos = bilby.gw.detector.InterferometerList([pars['det']])

    # set noise to be colored Gaussian noise
    ifos.set_strain_data_from_power_spectral_densities(
        sampling_frequency=sampling_frequency, duration=duration,
        start_time=injection_parameters['geocent_time'] - 3)

    # inject signal
    signal_noise = ifos[0].strain_data.frequency_domain_strain
    ifos.inject_signal(waveform_generator=waveform_generator,
                       parameters=injection_parameters)
    

    whiten_hp = freq_signal['plus']/ifos[0].amplitude_spectral_density_array
    whiten_hc = freq_signal['cross']/ifos[0].amplitude_spectral_density_array

    hp = np.fft.irfft(whiten_hp,int(duration*sampling_frequency))
    hc = np.fft.irfft(whiten_hc,int(duration*sampling_frequency))

    # TODO: may need to include event time in here somehow
    #hp_rolled = np.pad(hp,(0,int(0.5*sampling_frequency)), mode='constant')[int(0.5*sampling_frequency):]
    #hc_rolled = np.pad(hc,(0,int(0.5*sampling_frequency)), mode='constant')[int(0.5*sampling_frequency):]
    hp_rolled = np.roll(hp.reshape(hp.shape[0],1),int(-0.5*sampling_frequency))
    hc_rolled = np.roll(hc.reshape(hc.shape[0],1),int(-0.5*sampling_frequency))

    ht_shift, hp_shift, hc_shift = make_bbh(hp_rolled,hc_rolled,sampling_frequency,pars['ra'],pars['dec'],pars['psi'],pars['det'],ifos[0],injection_parameters['geocent_time'])

    # make aggressive window to cut out signal in central region
    # window is non-flat for 1/8 of desired Tobs
    # the window has dropped to 50% at the Tobs boundaries
    N = int(duration*sampling_frequency)
    safe = 2.0                       # define the safe multiplication scale for the desired time length
    win = np.zeros(N)
    tempwin = tukey(int((16.0/15.0)*N/safe),alpha=1.0/8.0)
    win[int((N-tempwin.size)/128):int((N-tempwin.size)/128)+tempwin.size] = tempwin

    # apply aggressive window to cut out signal in central region
    # window is non-flat for 1/8 of desired Tobs
    # the window has dropped to 50% at the Tobs boundaries
    ht_shift=ht_shift.reshape(ht_shift.shape[0])
    ht_shift[:] *= win
    ht = ht_shift

    # strain data is noise. wfg is signal


    # how to get sig+noise time series and whitened sig+noise time series
    # ifos[0].strain_data.time_domain_strain
    # np.fft.irfft(ifos[0].whitened_frequency_domain_strain)

    # whiten noise
    white_noise_sig = np.fft.irfft(signal_noise/ifos[0].amplitude_spectral_density_array,(int(duration*sampling_frequency)))

    # combine noise and noise-free signal
    ht_noisy = ht + white_noise_sig 
    #plt.plot(white_noise_sig+ht_shift, alpha=0.5)
    #plt.plot(white_noise_sig, alpha=0.5)
    #plt.savefig('/home/hunter.gabbard/public_html/test.png')
    #plt.close()
    #print('Done')
    #exit()


    return ht,ht_noisy,injection_parameters,ifos,waveform_generator

def gen_masses(m_min=5.0,M_max=100.0,mdist='astro'):
    """ function returns a pair of masses drawn from the appropriate distribution
   
    Parameters
    ----------
    m_min:
        minimum component mass
    M_max:
        maximum total mass
    mdist:
        mass distribution to use when generating templates
    Returns
    -------
    m12: list
        both component mass parameters
    eta:
        eta parameter
    mc:
        chirp mass parameter
    """
    
    flag = False

    if mdist=='astro':
        print('{}: using astrophysical logarithmic mass distribution'.format(time.asctime()))
        new_m_min = m_min
        new_M_max = M_max
        log_m_max = np.log(new_M_max - new_m_min)
        while not flag:
            m12 = np.exp(np.log(new_m_min) + np.random.uniform(0,1,2)*(log_m_max-np.log(new_m_min)))
            flag = True if (np.sum(m12)<new_M_max) and (np.all(m12>new_m_min)) and (m12[0]>=m12[1]) else False
        eta = m12[0]*m12[1]/(m12[0]+m12[1])**2
        mc = np.sum(m12)*eta**(3.0/5.0)
        return m12, mc, eta

def gen_par(fs,T_obs,geocent_time,mdist='astro'):
    """ Generates a random set of parameters
    
    Parameters
    ----------
    fs:
        sampling frequency (Hz)
    T_obs:
        observation time window (seconds)
    mdist:
        distribution of masses to use
    beta:
        fractional allowed window to place time series
    gw_tmp:
        if True: generate an event-like template
    Returns
    -------
    par: class object
        class containing parameters of waveform
    """
    # define distribution params
    m_min = 5.0         # 5 rest frame component masses
    M_max = 100.0       # 100 rest frame total mass

    m12, mc, eta = gen_masses(m_min,M_max,mdist=mdist)
    M = np.sum(m12)
    print('{}: selected bbh masses = {},{} (chirp mass = {})'.format(time.asctime(),m12[0],m12[1],mc))

    # generate reference phase
    phase = 2.0*np.pi*np.random.rand()
    print('{}: selected bbh reference phase = {}'.format(time.asctime(),phase))

    geocent_time = np.random.uniform(low=geocent_time-0.5,high=geocent_time+0.5)
    print('{}: selected bbh GPS time = {}'.format(time.asctime(),geocent_time))

    lum_dist = np.random.uniform(low=1e2, high=4e2)
    print('{}: selected bbh luminosity distance = {}'.format(time.asctime(),lum_dist))

    return m12[0], m12[1], mc, eta, phase, geocent_time, lum_dist

def run(sampling_frequency=1024.,duration=1.,m1=36.,m2=36.,
           geocent_time=1126259642.5,lum_dist=2000.,phase=1.3,N_gen=1000,make_test_samp=False,
           make_train_samp=False,run_label='test_results'):
    # Set the duration and sampling frequency of the data segment that we're
    # going to inject the signal into
    duration = duration
    sampling_frequency = sampling_frequency
    det='H1'
    ra=1.375
    dec=-1.2108
    psi=2.659
    theta_jn=0.4
    lum_dist=lum_dist
    mc=0
    eta=0

    pars = {'m1':m1,'m2':m2,'geocent_time':geocent_time,'phase':phase,
            'N_gen':N_gen,'det':det,'ra':ra,'dec':dec,'psi':psi,'theta_jn':theta_jn,'lum_dist':lum_dist}

    # Specify the output directory and the name of the simulation.
    outdir = 'gw_data/bilby_output'
    label = run_label
    bilby.core.utils.setup_logger(outdir=outdir, label=label)

    # Set up a random seed for result reproducibility.  This is optional!
    np.random.seed(88170235)

    # We are going to inject a binary black hole waveform.  We first establish a
    # dictionary of parameters that includes all of the different waveform
    # parameters, including masses of the two black holes (mass_1, mass_2),
    # spins of both black holes (a, tilt, phi), etc.

    # generate training samples
    if make_train_samp == True:
        train_samples = []
        train_pars = []
        for i in range(N_gen):
            # choose waveform parameters here
            pars['m1'], pars['m2'], mc,eta, pars['phase'], pars['geocent_time'], pars['lum_dist']=gen_par(duration,sampling_frequency,geocent_time)
            train_samples.append(gen_template(duration,sampling_frequency,
                                   pars)[0:2])
            train_pars.append([mc,pars['lum_dist'],pars['phase'],pars['geocent_time']])
            print('Made waveform %d/%d' % (i,N_gen))
        train_samples_noisefree = np.array(train_samples)[:,0,:]
        train_samples_noisy = np.array(train_samples)[:,1,:]
        return train_samples_noisy,train_samples_noisefree,np.array(train_pars)

    # generate testing sample 
    opt_snr = 0       
    if make_test_samp == True:
        # ensure that signal is loud enough (e.g. > detection threshold)
        while opt_snr < 8:
            # generate parameters
            pars['m1'], pars['m2'], mc,eta, pars['phase'], pars['geocent_time'], pars['lum_dist']=gen_par(duration,sampling_frequency,geocent_time)
            # make parameters have equal mass
            pars['m1']=pars['m2']
            # inject signal
            test_samp_noisefree,test_samp_noisy,injection_parameters,ifos,waveform_generator = gen_template(duration,sampling_frequency,
                                   pars)

            opt_snr = ifos[0].meta_data['optimal_SNR']
            print(ifos[0].meta_data['optimal_SNR'])
    # Set up a PriorDict, which inherits from dict.
    # By default we will sample all terms in the signal models.  However, this will
    # take a long time for the calculation, so for this example we will set almost
    # all of the priors to be equall to their injected values.  This implies the
    # prior is a delta function at the true, injected value.  In reality, the
    # sampler implementation is smart enough to not sample any parameter that has
    # a delta-function prior.
    # The above list does *not* include mass_1, mass_2, theta_jn and luminosity
    # distance, which means those are the parameters that will be included in the
    # sampler.  If we do nothing, then the default priors get used.
    priors = bilby.gw.prior.BBHPriorDict()
    priors['geocent_time'] = bilby.core.prior.Uniform(
        minimum=injection_parameters['geocent_time'] - duration/2,
        maximum=injection_parameters['geocent_time'] + duration/2,
        name='geocent_time', latex_label='$t_c$', unit='$s$')
    # fix the following parameter priors
    priors['a_1'] = 0
    priors['a_2'] = 0
    priors['tilt_1'] = 0
    priors['tilt_2'] = 0
    priors['phi_12'] = 0
    priors['phi_jl'] = 0
    priors['ra'] = 1.375
    priors['dec'] = -1.2108
    priors['psi'] = 2.659
    priors['theta_jn'] = 0.4
    priors['luminosity_distance'] =  bilby.gw.prior.UniformComovingVolume(name='luminosity_distance', minimum=1e2, maximum=4e2, unit='Mpc')

    # all pars not included from list above will have pe done on them
    for key in ['a_1', 'a_2', 'tilt_1', 'tilt_2', 'phi_12', 'phi_jl', 'theta_jn', 'psi', 'ra',
                'dec']:
        priors[key] = injection_parameters[key]

    # Initialise the likelihood by passing in the interferometer data (ifos) and
    # the waveform generator
    likelihood = bilby.gw.GravitationalWaveTransient(
        interferometers=ifos, waveform_generator=waveform_generator,
        priors=priors)

    # Run sampler.  In this case we're going to use the `dynesty` sampler
    #dynesty=bilby.core.sampler.dynesty.Dynesty(likelihood=likelihood,priors=priors,dlogz=30.)
    result = bilby.run_sampler(dlogz=300.,
        likelihood=likelihood, priors=priors, sampler='dynesty', npoints=500,
        injection_parameters=injection_parameters, outdir=outdir, label=label,
        save='hdf5')

    # Make a corner plot.
    result.plot_corner()

    # save test sample waveform
    hf = h5py.File('%s/test_sample-%s.h5py' % (outdir,run_label), 'w')
    hf.create_dataset('noisy_waveform', data=test_samp_noisy)
    hf.create_dataset('noisefree_waveform', data=test_samp_noisefree)
    hf.close()

    print('finished running pe')

