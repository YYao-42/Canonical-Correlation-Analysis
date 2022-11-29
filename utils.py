import numpy as np
import random
import os
import mne
import scipy.io
from sklearn.covariance import LedoitWolf
from tqdm import tqdm
from numpy import linalg as LA
from scipy import signal
from scipy.linalg import toeplitz, eig, eigh
from scipy.sparse.linalg import eigs
from scipy.stats import zscore, pearsonr
from numba import jit


def eig_sorted(X, option='descending'):
    '''
    Eigenvalue decomposition, with ranked eigenvalues
    X = V @ np.diag(lam) @ LA.inv(V)
    Could be replaced by eig in scipy.linalg
    '''
    lam, V = LA.eig(X)
    # lam = np.real(lam)
    # V = np.real(V)
    if option == 'descending':
        idx = np.argsort(-lam)
    elif option =='ascending':
        idx = np.argsort(lam)
    else:
        idx = range(len(lam))
        print('Warning: Not sorted')
    lam = lam[idx] # rank eigenvalues
    V = V[:, idx] # rearrange eigenvectors accordingly
    return lam, V


def PCAreg_inv(X, rank):
    '''
    PCA Regularized inverse of a symmetric square matrix X
    rank could be a smaller number than rank(X)
    '''
    lam, V = eig_sorted(X)
    lam = lam[:rank]
    V = V[:, :rank]
    inv = V @ np.diag(1/lam) @ np.transpose(V)
    return inv


def convolution_mtx(L_timefilter, x, causal=True):
    '''
    Calculate the convolution matrix
    Convolution: y(t)=x(t)*h(t)
    In matrix form: y=Xh E.g. time lag = 3
    If causal,
    h = h(0); h(1); h(2)
    X = 
    x(0)   x(-1)  x(-2)
    x(1)   x(0)   x(-1)
            ...
    x(T-1) x(T-2) x(T-3)
    If non-causal,
    h = h(-1); h(0); h(1)
    X = 
    x(1)   x(0)   x(-1)
    x(2)   x(1)   x(0)
            ...
    x(T)   x(T-1) x(T-2)
    Unknown values are set as 0
    '''
    first_col = np.zeros(L_timefilter)
    first_col[0] = x[0]
    if causal:
        conv_mtx = np.transpose(toeplitz(first_col, x))
    else:
        assert(L_timefilter % 2 == 1)
        L = int((L_timefilter-1)/2)
        x = np.append(x, [np.zeros((1,L))])
        conv_mtx = np.transpose(toeplitz(first_col, x))
        conv_mtx = conv_mtx[L:,:]
    return conv_mtx


def block_Hankel(X, L, causal=False):
    '''
    For spatial-temporal filter, calculate the block Hankel matrix
    Inputs:
    X: T(#sample)xD(#channel)
    L: number of time lags; from -(L-1) to 0 (causal) or -(L-1)/2 to (L-1)/2 (non-causal)
    causal: default false
    '''
    Hankel_list = [convolution_mtx(L, X[:,i], causal=causal) for i in range(X.shape[1])]
    blockHankel = np.concatenate(tuple(Hankel_list), axis=1)
    return blockHankel


def split(EEG, Sti, fold=10, fold_idx=1):
    T = EEG.shape[0]
    len_test = T // fold
    if np.ndim(EEG)==2:
        EEG_test = EEG[len_test*(fold_idx-1):len_test*fold_idx,:]
        EEG_train = np.delete(EEG, range(len_test*(fold_idx-1), len_test*fold_idx), axis=0)
    elif np.ndim(EEG)==3:
        EEG_test = EEG[len_test*(fold_idx-1):len_test*fold_idx,:,:]
        EEG_train = np.delete(EEG, range(len_test*(fold_idx-1), len_test*fold_idx), axis=0)
    else:
        print('Warning: Check the dimension of EEG data')
    Sti_test = Sti[len_test*(fold_idx-1):len_test*fold_idx]
    Sti_train = np.delete(Sti, range(len_test*(fold_idx-1), len_test*fold_idx), axis=0)
    return EEG_train, EEG_test, Sti_train, Sti_test


def split_multi_mod(datalist, fold=10, fold_idx=1):
    train_list = []
    test_list = []
    for data in datalist:
        T = data.shape[0]
        len_test = T // fold
        if np.ndim(data)==2:
            data_test = data[len_test*(fold_idx-1):len_test*fold_idx,:]
            data_train = np.delete(data, range(len_test*(fold_idx-1), len_test*fold_idx), axis=0)
        elif np.ndim(data)==3:
            data_test = data[len_test*(fold_idx-1):len_test*fold_idx,:,:]
            data_train = np.delete(data, range(len_test*(fold_idx-1), len_test*fold_idx), axis=0)
        else:
            print('Warning: Check the dimension of data')
        train_list.append(data_train)
        test_list.append(data_test)
    return train_list, test_list


def corr_component(X, n_components, W_train=None):
    '''
    Inputs:
    X: EEG data with shape (T, D, N) [T: # sample, D: # channel, N: # subjects]
    n_components: number of components
    W_train: If not None, then goes to test mode.
    Outputs:
    ISC: inter-subject correlation
    W: weights
    '''
    _, D, N = X.shape
    Rw = np.zeros([D,D])
    for n in range(N):
        Rw += np.cov(np.transpose(X[:,:,n])) # Inside np.cov: observations in the columns
    Rt = N**2*np.cov(np.transpose(np.average(X, axis=2)))
    Rb = (Rt - Rw)/(N-1)
    rank = LA.matrix_rank(Rw)
    if rank < D:
        invRw = PCAreg_inv(Rw, rank)
    else:
        invRw = LA.inv(Rw)
    if W_train is not None: # Test mode
        W = W_train
        ISC = np.diag((np.transpose(W)@Rb@W)/(np.transpose(W)@Rw@W))
    else: # Train mode
        ISC, W = eig_sorted(invRw@Rb)
    # TODO: ISC here is an approximation of real average pairwise correlation
    return ISC[:n_components], W[:,:n_components]
    

def cano_corr(X, Y, n_components = 5, regularizaion=None, K_regu=None, V_A=None, V_B=None):
    '''
    Input:
    X: EEG data T(#sample)xD(#channel) or TxDL if use spatial-temporal filter
    Y: Stimulus T(#sample)xL(#tap)
    '''
    _, D = X.shape
    _, L = Y.shape
    if V_A is not None: # Test Mode
        flag_test = True
    else: # Train mode
        flag_test = False
        # compute covariance matrices
        covXY = np.cov(X, Y, rowvar=False)
        if regularizaion=='lwcov':
            Rx = LedoitWolf().fit(X).covariance_
            Ry = LedoitWolf().fit(Y).covariance_
        else:
            Rx = covXY[:D,:D]
            Ry = covXY[D:D+L,D:D+L]
        Rxy = covXY[:D,D:D+L]
        Ryx = covXY[D:D+L,:D]
        # PCA regularization is recommended (set K_regu<rank(Rx))
        # such that the small eigenvalues dominated by noise are discarded
        if K_regu is None:
            invRx = PCAreg_inv(Rx, LA.matrix_rank(Rx))
            invRy = PCAreg_inv(Ry, LA.matrix_rank(Ry))
        else:
            K_regu = min(LA.matrix_rank(Rx), LA.matrix_rank(Ry), K_regu)
            invRx = PCAreg_inv(Rx, K_regu)
            invRy = PCAreg_inv(Ry, K_regu)
        A = invRx@Rxy@invRy@Ryx
        B = invRy@Ryx@invRx@Rxy
        # lam of A and lam of B should be the same
        # can be used as a preliminary check for correctness
        # the correlation coefficients are already available by taking sqrt of the eigenvalues: corr_coe = np.sqrt(lam[:K_regu])
        # or we do the following to obtain transformed X and Y and calculate corr_coe from there
        _, V_A = eig_sorted(A)
        _, V_B = eig_sorted(B)
        V_A = np.real(V_A[:,:n_components])
        V_B = np.real(V_B[:,:n_components])
    X_trans = X@V_A
    Y_trans = Y@V_B
    corr_pvalue = [pearsonr(X_trans[:,k], Y_trans[:,k]) for k in range(n_components)]
    corr_coe = np.array([corr_pvalue[k][0] for k in range(n_components)])
    # P-value-null hypothesis: the distributions underlying the samples are uncorrelated and normally distributed.
    p_value = np.array([corr_pvalue[k][1] for k in range(n_components)])
    if not flag_test:
        # to match filters v_a and v_b s.t. corr_coe is always positive
        V_A[:,corr_coe<0] = -1*V_A[:,corr_coe<0]
        corr_coe[corr_coe<0] = -1*corr_coe[corr_coe<0]
    return corr_coe, p_value, V_A, V_B


def GCCA(X_stack, n_components, regularization='lwcov', W_train=None):
    '''
    Inputs:
    X_stack: stacked (along axis 2) data of different subjects
    n_components: number of components
    regularization: regularization method when estimating covariance matrices (Default: LedoitWolf)
    W_train: If not None, then goes to test mode.
    Outputs:
    lam: eigenvalues, related to mean squared error (not used in analysis)
    W_stack: (rescaled) weights with shape (D*N*n_components)
    avg_corr: average pairwise correlation
    '''
    _, D, N = X_stack.shape
    # From [X1; X2; ... XN] to [X1 X2 ... XN]
    # each column represents a variable, while the rows contain observations
    X_list = [X_stack[:,:,n] for n in range(N)]
    X = np.concatenate(tuple(X_list), axis=1)
    if regularization == 'lwcov':
        Rxx = LedoitWolf().fit(X).covariance_
    else:
        Rxx = np.cov(X, rowvar=False)
        # In case Rxx is not semi-definite positive 
        lam_Rxx, W_Rxx = eigh(Rxx)
        if lam_Rxx[0]<0:
            lam_Rxx = lam_Rxx-lam_Rxx[0]
        Rxx = W_Rxx@np.diag(lam_Rxx)@W_Rxx.T
    Dxx = np.zeros_like(Rxx)
    for n in range(N):
        Dxx[n*D:(n+1)*D,n*D:(n+1)*D] = Rxx[n*D:(n+1)*D,n*D:(n+1)*D]
    if W_train is not None: # Test mode
        W = np.transpose(W_train,(1,0,2))
        W = np.reshape(W, [N*D,n_components])
        lam = np.diag(np.transpose(W)@Dxx@W)
    else: # Train mode
        # Dxx and Rxx are symmetric matrices, so here we can use eigh
        # Otherwise we should use eig, which is much slower
        # Generalized eigenvalue decomposition
        # Dxx @ W = Rxx @ W @ np.diag(lam)
        # Dxx @ W[:,i] = lam[i] * Rxx @ W[:,i]
        lam, W = eigh(Dxx, Rxx, subset_by_index=[0,n_components-1]) # automatically ascend
        # lam also equals to np.diag(np.transpose(W)@Dxx@W)
    W_stack = np.reshape(W, (N,D,-1))
    W_stack = np.transpose(W_stack, [1,0,2]) # W: D*N*n_components
    # Rescale weights such that the average pairwise correlation can be calculated using efficient matrix operations
    # Alternatively, just call function avg_corr_coe
    W_stack = rescale(W_stack, Dxx)
    W_scaled = np.transpose(W_stack,(1,0,2))
    W_scaled = np.reshape(W_scaled, [N*D,n_components])
    avg_corr = np.diag(np.transpose(W_scaled)@(Rxx-Dxx)@W_scaled)/np.diag(np.transpose(W_scaled)@Dxx@W_scaled)/(N-1)
    return lam, W_stack, avg_corr


def GCCA_multi_modal(datalist, n_components, rhos, regularization='lwcov'):
    '''
    Inputs:
    datalist: data of different modalities (a list) E.g, [EEG_stack, Stim]
    n_components: number of components
    rhos: controls the weights of different modalities; should have the same length as the datalist
    regularization: regularization method when estimating covariance matrices (Default: LedoitWolf)
    Outputs:
    Wlist: weights of different modalities (a list)
    '''
    dim_list = []
    flatten_list = []
    rho_list = []
    for i in range(len(datalist)):
        data = datalist[i]
        rho = rhos[i]
        if np.ndim(data) == 3:
            _, D, N = data.shape
            X_list = [data[:,:,n] for n in range(N)]
            X = np.concatenate(tuple(X_list), axis=1)
            flatten_list.append(X)
            dim_list = dim_list + [D]*N
            rho_list = rho_list + [rho]*(D*N)
        elif np.ndim(data) == 2:
            _, L = data.shape
            flatten_list.append(data)
            dim_list.append(L)
            rho_list = rho_list + [rho]*L
        else:
            print('Warning: Check dim of data')
    X_mm = np.concatenate(tuple(flatten_list), axis=1)
    Rxx = np.cov(X_mm, rowvar=False)
    Dxx = np.zeros_like(Rxx)
    dim_accumu = 0
    for dim in dim_list:
        if regularization == 'lwcov':
            Rxx[dim_accumu:dim_accumu+dim, dim_accumu:dim_accumu+dim] = LedoitWolf().fit(X_mm[:,dim_accumu:dim_accumu+dim]).covariance_
        Dxx[dim_accumu:dim_accumu+dim, dim_accumu:dim_accumu+dim] = Rxx[dim_accumu:dim_accumu+dim, dim_accumu:dim_accumu+dim]
        dim_accumu = dim_accumu + dim
    Rxx = Rxx*np.expand_dims(np.array(rho_list), axis=0)
    # Dxx and Rxx are symmetric matrices, so here we can use eigh
    # Otherwise we should use eig, which is much slower
    # Generalized eigenvalue decomposition
    # Dxx @ W = Rxx @ W @ np.diag(lam)
    # Dxx @ W[:,i] = lam[i] * Rxx @ W[:,i]
    # lam, W = eigh(Dxx, Rxx, subset_by_index=[0,n_components-1]) # automatically ascend
    # lam, W = eigs(Dxx, n_components, Rxx, which='SR')
    # W = np.real(W)
    lam, W = eig(Dxx, Rxx)
    idx = np.argsort(lam)
    lam = lam[idx] # rank eigenvalues
    W = np.real(W[:, idx]) # rearrange eigenvectors accordingly
    # lam also equals to np.diag(np.transpose(W)@Dxx@W)
    Wlist = W_organize(W[:,:n_components], datalist)
    return Wlist


def W_organize(W, datalist):
    W_list = []
    dim_start = 0
    for data in datalist:
        if np.ndim(data) == 3:
            _, D, N = data.shape
            dim_end = dim_start + D*N
            W_temp = W[dim_start:dim_end,:]
            W_stack = np.reshape(W_temp, (N,D,-1))
            W_list.append(np.transpose(W_stack, [1,0,2]))
        elif np.ndim(data) == 2:
            _, D = data.shape
            dim_end = dim_start + D
            W_list.append(W[dim_start:dim_end,:])
        else:
            print('Warning: Check the dim of data')
        dim_start = dim_end
    return W_list


def forward_model(X, W, regularization=None):
    '''
    Reference: On the interpretation of weight vectors of linear models in multivariate neuroimaging https://www.sciencedirect.com/science/article/pii/S1053811913010914
    Backward models: Extract latent factors as functions of the observed data s(t) = W^T x(t)
    Forward models: Reconstruct observations from latent factors x(t) = As(t) + n(t)
    x(t): D-dimensional observations
    s(t): K-dimensional latent factors
    W: backward model
    A: forward model

    In our use case the backward model can be found using (G)CCA. Latent factors are the representations generated by different components.
    The forward model has a general form (see the reference): A = Rxx W inv(Rss) = Rxx W inv(W^T Rxx W)

    Inputs:
    X: observations (one subject) TxD
    W: filters/backward models DxK
    regularization: lwcov or none

    Output:
    A: forward model
    '''
    if regularization == 'lwcov':
        Rxx = LedoitWolf().fit(X).covariance_
    else:
        Rxx = np.cov(X, rowvar=False)
    A = Rxx@W@LA.inv(W.T@Rxx@W)
    return A


def rescale(W, Dxx):
    '''
    To make w_n^H R_{xn xn} w_n = 1 for all n. Then the denominators of correlation coefficients between every pairs are the same.
    '''
    _, N, n_componets = W.shape
    for i in range(n_componets):
        W_split = np.split(W[:,:,i], N, axis=1)
        W_blkdiag = scipy.sparse.block_diag(W_split)
        scales = np.diag(np.transpose(W_blkdiag)@Dxx@W_blkdiag)
        W[:,:,i] = W[:,:,i]/np.sqrt(scales)
    return W


def avg_corr_coe(X_stack, W, N, n_components=5):
    '''
    Calculate the pairwise average correlation.
    Inputs:
    X_stack: stacked (along axis 2) data of different subjects (or even modalities)
    W: weights 1) dim(W)=2: results of correlated component analysis 2) dim(W)=3: results of GCCA
    N: number of datasets
    n_components: number of components
    Output:
    avg_corr: average pairwise correlation
    '''
    avg_corr = np.zeros(n_components)
    if np.ndim (W) == 2:
        W = np.expand_dims(W, axis=1)
        W = np.repeat(W, N, axis=1)
    for component in range(n_components):
        w = W[:,:,component]
        w = np.expand_dims(w, axis=1)
        X_trans = np.einsum('tdn,dln->tln', X_stack, w)
        X_trans = np.squeeze(X_trans, axis=1)
        corr_mtx = np.corrcoef(X_trans, rowvar=False)
        avg_corr[component] = np.sum(corr_mtx-np.eye(N))/N/(N-1)
    return avg_corr


def avg_corr_coe_multi_modal(datalist, Wlist, n_components=5, regularization=None):
    '''
    Calculate the pairwise average correlation.
    Inputs:
    datalist: data of different modalities (a list) E.g, [EEG_stack, Stim]
    Wlist: weights of different modalities (a list)
    n_components: number of components
    Output:
    avg_corr: average pairwise correlation
    '''
    avg_corr = np.zeros(n_components)
    n_mod = len(datalist)
    for component in range(n_components):
        X_trans_list = []
        for i in range(n_mod):
            W = Wlist[i]
            if np.ndim(W) == 3:
                w = W[:,:,component]
                w = np.expand_dims(w, axis=1)
                X_trans = np.einsum('tdn,dln->tln', datalist[i], w)
                X_trans = np.squeeze(X_trans, axis=1)
            if np.ndim(W) == 2:
                w = W[:,component]
                X_trans = datalist[i]@w
                X_trans = np.expand_dims(X_trans, axis=1)
            X_trans_list.append(X_trans)
        X_trans_all = np.concatenate(tuple(X_trans_list), axis=1)
        if regularization=='lwcov':
            cov_mtx = LedoitWolf().fit(X_trans_all).covariance_
            cov_diag = np.expand_dims(np.diag(cov_mtx).shape, axis = 1)
            corr_mtx = cov_mtx/np.sqrt(cov_diag)/np.sqrt(cov_diag.T)
        else:
            corr_mtx = np.corrcoef(X_trans_all, rowvar=False)
        N = X_trans_all.shape[1]
        avg_corr[component] = np.sum(corr_mtx-np.eye(N))/N/(N-1)
    return avg_corr


def cross_val_CCA(EEG, feature, L_timefilter, fs, fold=10, n_components=5, regularizaion='lwcov', K_regu=None, message=True, signifi_level=True):
    corr_train = np.zeros((fold, n_components))
    corr_test = np.zeros((fold, n_components))
    for idx in range(fold):
        EEG_train, EEG_test, Sti_train, Sti_test = split(EEG, feature, fold=fold, fold_idx=idx+1)
        conv_mtx_train = convolution_mtx(L_timefilter, Sti_train)
        corr_train[idx,:], _, V_A_train, V_B_train = cano_corr(EEG_train, conv_mtx_train, n_components=n_components, regularizaion=regularizaion, K_regu=K_regu)
        conv_mtx_test = convolution_mtx(L_timefilter, Sti_test)
        corr_test[idx,:], _, _, _ = cano_corr(EEG_test, conv_mtx_test, n_components=n_components, V_A=V_A_train, V_B=V_B_train)
    if signifi_level:
        corr_trials = permutation_test(EEG_test, np.squeeze(Sti_test), num_test=1000, t=0.1, fs=fs, topK=n_components, V_A=V_A_train, V_B=V_B_train)
        corr_trials = np.sort(abs(corr_trials), axis=0)
        print('Significance level of each component: {}'.format(corr_trials[-10,:]))
    if message:
        print('Average correlation coefficients of the top {} components on the training sets: {}'.format(n_components, np.average(corr_train, axis=0)))
        print('Average correlation coefficients of the top {} components on the test sets: {}'.format(n_components, np.average(corr_test, axis=0)))
    return corr_train, corr_test, V_A_train, V_B_train


def cross_val_GCCA_multi_mod(datalist, L_timefilter, rhos, fs, fold=10, n_components=5, regularizaion='lwcov', message=True, signifi_level=True):
    n_mod = len(datalist)
    corr_train = np.zeros((fold, n_components))
    corr_test = np.zeros((fold, n_components))
    for idx in range(fold):
        train_list, test_list = split_multi_mod(datalist, fold=fold, fold_idx=idx+1)
        for i in range(1, n_mod):
            train_list[i] = convolution_mtx(L_timefilter, train_list[i])
            test_list[i] = convolution_mtx(L_timefilter, test_list[i])
        Wlist_train = GCCA_multi_modal(train_list, n_components=n_components, rhos=rhos, regularization=regularizaion)
        corr_train[idx,:] = avg_corr_coe_multi_modal(train_list, Wlist_train, n_components=n_components)
        corr_test[idx,:] = avg_corr_coe_multi_modal(test_list, Wlist_train, n_components=n_components)
    if signifi_level:
        corr_trials = permutation_test_multi_mod(test_list, num_test=1000, t=0.1, fs=fs, topK=n_components, Wlist=Wlist_train)
        corr_trials = np.sort(abs(corr_trials), axis=0)
        print('Significance level of each component: {}'.format(corr_trials[-10,:]))
    if message:
        print('Average correlation coefficients of the top {} components on the training sets: {}'.format(n_components, np.average(corr_train, axis=0)))
        print('Average correlation coefficients of the top {} components on the test sets: {}'.format(n_components, np.average(corr_test, axis=0)))
    return corr_train, corr_test, Wlist_train


def shuffle_block(X, t, fs):
    block_len = int(t*fs)
    T, D, N = X.shape
    append_arr = np.zeros((block_len-T%block_len, D, N))
    X = np.concatenate((X, append_arr), axis=0)
    T_appended = X.shape[0]
    X_shuffled = np.zeros_like(X)
    for n in range(N):
        blocks = [X[i:i+block_len, :, n] for i in range(0, T_appended, block_len)]
        random.shuffle(blocks)
        X_shuffled[:,:,n] = np.concatenate(tuple(blocks), axis=0)
    return X_shuffled


def permutation_test(X, Y, num_test, t, fs, topK, V_A=None, V_B=None):
    corr_coe_topK = np.empty((0, topK))
    X = np.expand_dims(X, axis=2)
    Y = np.expand_dims(Y, axis=(1,2))
    for i in tqdm(range(num_test)):
        X_shuffled = np.squeeze(shuffle_block(X, t, fs), axis=2)
        Y_shuffled = np.squeeze(shuffle_block(Y, t, fs), axis=(1,2))
        L_timefilter = fs
        conv_mtx_shuffled = convolution_mtx(L_timefilter, Y_shuffled)
        if V_A is not None:
            X_shuffled_trans = X_shuffled@V_A
            Y_shuffled_trans = conv_mtx_shuffled@V_B
            K_regu = min(V_A.shape[1], V_B.shape[1])
            corr_pvalue = [pearsonr(X_shuffled_trans[:,k], Y_shuffled_trans[:,k]) for k in range(K_regu)]
            corr_coe = np.array([corr_pvalue[k][0] for k in range(K_regu)])
        else:
            corr_coe, _, _, _ = cano_corr(X_shuffled, conv_mtx_shuffled, regularizaion='lwcov')
        corr_coe_topK = np.concatenate((corr_coe_topK, np.expand_dims(corr_coe[:topK], axis=0)), axis=0)
    return corr_coe_topK


def permutation_test_multi_mod(datalist, num_test, t, fs, topK, Wlist):
    corr_coe_topK = np.empty((0, topK))
    for i in tqdm(range(num_test)):
        datalist_shuffled = []
        for data in datalist:
            if np.ndim(data) == 1:
                data_temp = np.expand_dims(data, axis=(1,2))
                data_temp = np.squeeze(shuffle_block(data_temp, t, fs), axis=(1,2))
                L_timefilter = fs
                datalist_shuffled.append(convolution_mtx(L_timefilter, data_temp))
            if np.ndim(data) == 2:
                data_temp = np.expand_dims(data, axis=2)
                data_temp = np.squeeze(shuffle_block(data_temp, t, fs), axis=2)
                datalist_shuffled.append(data_temp)
            elif np.ndim(data) == 3:
                datalist_shuffled.append(shuffle_block(data, t, fs))
        corr_coe = avg_corr_coe_multi_modal(datalist_shuffled, Wlist, n_components=5)
        corr_coe_topK = np.concatenate((corr_coe_topK, np.expand_dims(corr_coe[:topK], axis=0)), axis=0)
    return corr_coe_topK


def leave_one_fold_out(EEG, Sti, L_timefilter, K_regu=7, fold=10, fold_idx=1):
    EEG_train, EEG_test, Sti_train, Sti_test = split(EEG, Sti, fold=fold, fold_idx=fold_idx)
    conv_mtx_train = convolution_mtx(L_timefilter, Sti_train)
    corr_coe_train, p_value_train, V_A_train, V_B_train = cano_corr(EEG_train, conv_mtx_train)
    conv_mtx_test = convolution_mtx(L_timefilter, Sti_test)
    EEG_test_trans = EEG_test@V_A_train
    conv_mtx_test_trans = conv_mtx_test@V_B_train
    corr_pvalue = [pearsonr(EEG_test_trans[:,k], conv_mtx_test_trans[:,k]) for k in range(K_regu)]
    return corr_coe_train, corr_pvalue


def data_superbowl(head, datatype='preprocessed', year='2012', view='Y1'):
    path = head+'/'+datatype+'/'+year+'/'
    datafiles = os.listdir(path)
    X = []
    for datafile in datafiles:
        EEGdata = scipy.io.loadmat(path+datafile)
        fs = int(EEGdata['fsref'])
        data_per_subject = np.concatenate(tuple([EEGdata['Y1'][i][0] for i in range(len(EEGdata[view]))]),axis=1)
        data_per_subject = np.nan_to_num(data_per_subject, copy=False)
        X.append(np.transpose(data_per_subject))
    X = np.stack(tuple(X), axis=2)
    return X, fs


def rho_sweep(datalist, sweep_list, L_timefilter, fs, fold=10, n_components=5):
    corr_best = -np.Inf
    for i in sweep_list:
        rhos = [1, 10**i]
        _, corr_test = cross_val_GCCA_multi_mod(datalist, L_timefilter, rhos, fs, fold, n_components, regularizaion='lwcov', message=False, signifi_level=False)
        avg_corr_test = np.average(corr_test, axis=0)
        if avg_corr_test[0] > corr_best:
            rhos_best = rhos
            corr_best = avg_corr_test[0]
    return rhos_best


def EEG_normalization(data, len_seg):
    '''
    Normalize the EEG data.
    Subtract data of each channel by the mean of it
    Divide data into several segments, and for each segment, divide the data matrix by its Frobenius norm.
    Inputs:
    data: EEG data D x T
    len_seg: length of the segments
    Output:
    normalized_data
    '''
    _, T = data.shape
    n_blocks = T // len_seg + 1
    data_blocks = np.array_split(data, n_blocks, axis=1)
    data_zeromean = [db - np.mean(db, axis=1, keepdims=True) for db in data_blocks]
    normalized_blocks = [db/LA.norm(db) for db in data_zeromean]
    normalized_data = np.concatenate(tuple(normalized_blocks), axis=1)
    return normalized_data

def preprocessing(file_path, HP_cutoff = 0.5, AC_freqs=50, resamp_freqs=None, bads=[], eog=False, regression=False):
    '''
    Preprocessing of the raw signal
    Re-reference -> Highpass filter (-> downsample)
    No artifact removal technique has been applied yet
    Inputs:
    file_path: location of the eeg dataset
    HP_cutoff: cut off frequency of the high pass filter (for removing DC components and slow drifts)
    AC_freqs: AC power line frequency
    resamp_freqs: resampling frequency (if None then resampling is not needed)
    bads: list of bad channels
    eog: if contains 4 eog channels
    regression: whether regresses eog out
    Output:
    preprocessed: preprocessed eeg
    fs: the sample frequency of the EEG signal (original or down sampled)
    '''
    raw_lab = mne.io.read_raw_eeglab(file_path, preload=True)
    raw_lab.info['bads'] = bads
    fsEEG = raw_lab.info['sfreq']
    # If there are EOG channels, first treat them as EEG channels and do filterings and resamplings.
    if eog:
        misc_names = [raw_lab.info.ch_names[i] for i in mne.pick_types(raw_lab.info, misc=True)]
        type_eeg = ['eeg']*len(misc_names)
        change_type_dict = dict(zip(misc_names, type_eeg))
        raw_lab.set_channel_types(change_type_dict)
    # Highpass filter - remove DC components and slow drifts
    raw_highpass = raw_lab.copy().filter(l_freq=HP_cutoff, h_freq=None)
    # raw_highpass.compute_psd().plot(average=True)
    # Remove power line noise
    row_notch = raw_highpass.copy().notch_filter(freqs=AC_freqs)
    # row_notch.compute_psd().plot(average=True)
    # Resampling
    if resamp_freqs is not None:
        raw_downsampled = row_notch.copy().resample(sfreq=resamp_freqs)
        # raw_downsampled.compute_psd().plot(average=True)
        preprocessed = raw_downsampled
        fs = resamp_freqs
    else:
        preprocessed = row_notch
        fs = fsEEG
    # Then set EOG channels to their true type
    if eog:
        type_true = ['eog']*len(misc_names)
        change_type_dict = dict(zip(misc_names, type_true))
        preprocessed.set_channel_types(change_type_dict)
    # Rename channels and set montages
    biosemi_layout = mne.channels.read_layout('biosemi')
    ch_names_map = dict(zip(preprocessed.info['ch_names'], biosemi_layout.names))
    preprocessed.rename_channels(ch_names_map)
    montage = mne.channels.make_standard_montage('biosemi64')
    preprocessed.set_montage(montage)
    if len(bads)>0:
        # Interpolate bad channels
        preprocessed.interpolate_bads()
    # Re-reference
    # raw_lab.set_eeg_reference(ref_channels=['Cz']) # Select the reference channel to be Cz
    preprocessed.set_eeg_reference(ref_channels='average') # Apply an average reference
    if regression:
        EOGweights = mne.preprocessing.EOGRegression(picks='eeg', proj=False).fit(preprocessed)
        preprocessed = EOGweights.apply(preprocessed, copy=False)
    # Normalize EEG data
    eeg_channel_indices = mne.pick_types(preprocessed.info, eeg=True)
    eegdata, _ = preprocessed[eeg_channel_indices]
    preprocessed._data[eeg_channel_indices, :] = EEG_normalization(eegdata, 1*fs)
    return preprocessed, fs


def name_paths(eeg_path_head, feature_path_head):
    '''
    Find the name of the videos and the paths of the corresponding eeg signals and features
    Inputs:
    eeg_path_head: relative path of the eeg folder
    feature_path_head: relative path of the feature folder
    Output:
    videonames: names of the video
    eeg_sets_paths: relative paths of the eeg sets
    feature_sets_paths: relative paths of the feature sets
    '''
    eeg_list = os.listdir(eeg_path_head)
    eeg_sets = [i for i in eeg_list if i.endswith('.set')]
    eeg_sets_paths = [eeg_path_head+i for i in eeg_sets]
    feature_list = os.listdir(feature_path_head)
    feature_sets = [i for i in feature_list if i.endswith('.mat')]
    feature_sets_paths = [feature_path_head+i for i in feature_sets]
    videonames = [i[:-4] for i in eeg_sets]
    return videonames, eeg_sets_paths, feature_sets_paths


def load_eeg_feature(idx, videonames, eeg_sets_paths, feature_sets_paths, feature_type='muFlow', bads=[], eog=False, regression=False):
    '''
    Load the features and eeg signals of a specific dataset
    Inputs:
    idx: the index of the wanted dataset
    videonames: names of the video
    eeg_sets_paths: relative paths of the eeg sets
    feature_sets_paths: relative paths of the feature sets
    Outputs:
    eeg_downsampled: down sampled (and preprocessed) eeg signals
    normalized_features: normalized features 
    times: time axis 
    fsStim: sample rate of both stimulus and eeg signals
    '''
    # Load features and EEG signals
    videoname = videonames[idx]
    matching = [s for s in feature_sets_paths if videoname in s]
    assert len(matching) == 1
    features_data = scipy.io.loadmat(matching[0])
    fsStim = int(features_data['fsVideo']) # fs of the video 
    features = np.nan_to_num(features_data[feature_type]) # feature: optical flow
    eeg_prepro, _ = preprocessing(eeg_sets_paths[idx], HP_cutoff = 0.5, AC_freqs=50, resamp_freqs=fsStim, bads=bads, eog=eog, regression=regression)
    # Clip data
    eeg_channel_indices = mne.pick_types(eeg_prepro.info, eeg=True)
    eeg_downsampled, times = eeg_prepro[eeg_channel_indices]
    if len(features) > len(times):
        features = features[:len(times)]
    else:
        times = times[:len(features)]
        eeg_downsampled = eeg_downsampled[:,:len(features)]
    eeg_downsampled = eeg_downsampled.T
    # normalized_features = features/LA.norm(features)  # normalize features
    fs = fsStim
    # export_path = eeg_sets_paths[idx][:-4] + '.mat'
    # scipy.io.savemat(export_path, {'eeg'+videoname: eeg_downsampled.T, 'fs': fs})
    return eeg_downsampled, features, times, fs


def concatenate_eeg_feature(videonames, eeg_sets_paths, feature_sets_paths, feature_type='muFlow', bads=[], eog=False, regression=False):
    eeg_downsampled_list = []
    features_list = []
    for idx in range(len(videonames)):
        eeg_downsampled, features, _, fs = load_eeg_feature(idx, videonames, eeg_sets_paths, feature_sets_paths, feature_type, bads, eog, regression)
        eeg_downsampled_list.append(eeg_downsampled)
        features_list.append(features)
    eeg_concat = np.concatenate(eeg_downsampled_list, axis=0)
    features_concat = np.concatenate(features_list)
    times = np.array(range(len(features_concat)))/fs
    return eeg_concat, features_concat, times, fs


def load_eeg_env(idx, audionames, eeg_sets_paths, env_sets_paths, resamp_freq=20, band=[2, 9]):
    # Load features and EEG signals
    audioname = audionames[idx]
    matching = [s for s in env_sets_paths if audioname in s]
    assert len(matching) == 1
    envelope = np.squeeze(scipy.io.loadmat(matching[0])['envelope'])
    eeg_prepro, fsEEG = preprocessing(eeg_sets_paths[idx], HP_cutoff = 0.5, AC_freqs=50)
    # Clip data
    eeg_channel_indices = mne.pick_types(eeg_prepro.info, eeg=True)
    eeg, times = eeg_prepro[eeg_channel_indices]
    if len(envelope) > len(times):
        envelope = envelope[:len(times)]
    else:
        eeg = eeg[:,:len(envelope)]
    # Band-pass and down sample
    sos_bp = signal.butter(4, band, 'bandpass', output='sos', fs=fsEEG)
    eeg_filtered = signal.sosfilt(sos_bp, eeg)
    env_filtered = signal.sosfilt(sos_bp, envelope)
    eeg_downsampled = signal.resample_poly(eeg_filtered, resamp_freq, fsEEG, axis=1)
    env_downsampled = signal.resample_poly(env_filtered, resamp_freq, fsEEG)
    eeg_downsampled = eeg_downsampled.T
    times = np.array(range(len(eeg_downsampled)))/resamp_freq
    return eeg_downsampled, env_downsampled, times


def concatenate_eeg_env(audionames, eeg_sets_paths, env_sets_paths, resamp_freq=20, band=[2, 9]):
    eeg_downsampled_list = []
    env_downsampled_list = []
    for idx in range(len(audionames)):
        eeg_downsampled, env_downsampled, _ = load_eeg_env(idx, audionames, eeg_sets_paths, env_sets_paths, resamp_freq, band)
        eeg_downsampled_list.append(eeg_downsampled)
        env_downsampled_list.append(env_downsampled)
    # TODO: Do we need to normalize eeg signals when concatenating them?
    eeg_concat = np.concatenate(eeg_downsampled_list, axis=0)
    env_concat = np.concatenate(env_downsampled_list)
    times = np.array(range(len(env_concat)))/resamp_freq
    return eeg_concat, env_concat, times


def multisub_data_org(subjects, video, folder='EOG', feature_type=['muFlow'], bads=[], eog=False, regression=False):
    feature_path = '../../Experiments/Videos/stimuli/' + video + '_features.mat'
    features_data = scipy.io.loadmat(feature_path)
    fsStim = int(features_data['fsVideo']) # fs of the video 
    features_list = [np.abs(np.nan_to_num(features_data[type])) for type in feature_type]
    features = np.concatenate(tuple(features_list), axis=1)
    # features = features/LA.norm(features) # Normalize here or normalize the concatenated features?
    T = features.shape[0]
    eeg_list = []
    for sub in subjects:
        eeg_path = '../../Experiments/data/'+ sub +'/' + folder + '/' + video + '.set'
        eeg_prepro, fs = preprocessing(eeg_path, HP_cutoff = 0.5, AC_freqs=50, resamp_freqs=fsStim, bads=bads, eog=eog, regression=regression)
        eeg_channel_indices = mne.pick_types(eeg_prepro.info, eeg=True)
        eeg_downsampled, _ = eeg_prepro[eeg_channel_indices]
        eeg_downsampled = eeg_downsampled.T
        eeg_list.append(eeg_downsampled)
        if eeg_downsampled.shape[0] < T:
            T = eeg_downsampled.shape[0]
    # Clip data
    features = features[:T, :]
    eeg_list = [np.expand_dims(eeg[:T,:], axis=2) for eeg in eeg_list]
    eeg_multisub = np.concatenate(tuple(eeg_list), axis=2)
    times = np.array(range(T))/fs
    return features, eeg_multisub, fs, times