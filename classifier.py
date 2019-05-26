from sklearn.metrics import classification_report, accuracy_score
from collections import OrderedDict
import theano.tensor as T
import numpy as np
import lasagne
import theano
import argparse

def iterate_minibatches(inputs, targets, batch_size, shuffle=True):
    assert len(inputs) == len(targets)
    if shuffle:
        indices = np.arange(len(inputs))
        np.random.shuffle(indices)

    start_idx = None
    for start_idx in range(0, len(inputs) - batch_size + 1, batch_size):
        if shuffle:
            excerpt = indices[start_idx:start_idx + batch_size]
        else:
            excerpt = slice(start_idx, start_idx + batch_size)
        yield inputs[excerpt], targets[excerpt]

    if start_idx is not None and start_idx + batch_size < len(inputs):
        excerpt = indices[start_idx + batch_size:] if shuffle else slice(start_idx + batch_size, len(inputs))
        yield inputs[excerpt], targets[excerpt]


def get_nn_model(n_in, n_hidden, n_out, non_linearity):
    net = dict()
    non_lin = lasagne.nonlinearities.tanh
    if non_linearity == 'relu':
        non_lin = lasagne.nonlinearities.rectify
    net['input'] = lasagne.layers.InputLayer((None, n_in))
    net['fc'] = lasagne.layers.DenseLayer(
        net['input'],
        num_units=n_hidden,
        nonlinearity=non_lin)
    net['fc2'] = lasagne.layers.DenseLayer(
        net['fc'],
        num_units=n_hidden,
        nonlinearity=non_lin)
    net['output'] = lasagne.layers.DenseLayer(
        net['fc2'],
        num_units=n_out,
        nonlinearity=lasagne.nonlinearities.softmax)
    return net


def get_softmax_model(n_in, n_out):
    net = dict()
    net['input'] = lasagne.layers.InputLayer((None, n_in))
    net['output'] = lasagne.layers.DenseLayer(
        net['input'],
        num_units=n_out,
        nonlinearity=lasagne.nonlinearities.softmax)
    return net


def perturb(updates, params, learning_rate, sigma=0, noise=None, C=1):
    updates = OrderedDict(updates)
    for param in params:
        if noise == None:
            value = param.get_value(borrow=True)
            grad = param - updates[param]
            grad /= learning_rate
            val = lasagne.regularization.l2(grad)
            grad /= T.maximum(val / C, 1)
            updates[param] = param - learning_rate * grad - theano.shared(learning_rate * np.array(np.random.normal(0, sigma * C, value.shape), dtype=value.dtype), broadcastable=param.broadcastable)
        else:
            updates[param] -= theano.shared(learning_rate * noise[param], broadcastable=param.broadcastable)
    return updates


def train(dataset, hold_out_train_data=None, n_hidden=50, batch_size=100, epochs=100, learning_rate=0.01, model='nn', l2_ratio=1e-7,
        silent=True, non_linearity='relu', privacy='no_privacy', dp = 'dp', epsilon=0.5, delta=1e-5):
    train_x, train_y, test_x, test_y = dataset
    if hold_out_train_data != None:
        hold_out_x, hold_out_y, _, _ = hold_out_train_data
    n_in = train_x.shape[1]
    n_out = len(np.unique(train_y))

    if batch_size > len(train_y):
        batch_size = len(train_y)

    #print('Building model with {} training data, {} classes...'.format(len(train_x), n_out))
    input_var = T.matrix('x')
    target_var = T.ivector('y')
    if model == 'nn':
        #print('Using neural network...')
        net = get_nn_model(n_in, n_hidden, n_out, non_linearity)
    else:
        #print('Using softmax regression...')
        net = get_softmax_model(n_in, n_out)

    net['input'].input_var = input_var
    output_layer = net['output']

    # create loss function
    prediction = lasagne.layers.get_output(output_layer)
    loss = lasagne.objectives.categorical_crossentropy(prediction, target_var)
    loss = loss.mean() + l2_ratio * lasagne.regularization.regularize_network_params(output_layer, lasagne.regularization.l2)
    # create parameter update expressions
    params = lasagne.layers.get_all_params(output_layer, trainable=True)

    updates = lasagne.updates.adam(loss, params, learning_rate=learning_rate)

    train_fn = theano.function([input_var, target_var], loss, updates=updates, allow_input_downcast=True)
    test_prediction = lasagne.layers.get_output(output_layer, deterministic=True)
    test_fn = theano.function([input_var], test_prediction, allow_input_downcast=True)


    if hold_out_train_data != None:
        print('Training on hold out train data...')
        for epoch in range(epochs):
            loss_ = 0
            for input_batch, target_batch in iterate_minibatches(hold_out_x, hold_out_y, batch_size):
                loss_ += train_fn(input_batch, target_batch)
            loss_ = round(loss_, 3)
            if not silent:
                print('Epoch {}, train loss {}'.format(epoch, loss_))

        pred_y = []
        for input_batch, _ in iterate_minibatches(hold_out_x, hold_out_y, batch_size, shuffle=False):
            pred = test_fn(input_batch)
            pred_y.append(np.argmax(pred, axis=1))
        pred_y = np.concatenate(pred_y)

        if not silent:
            print('Hold Out Training Accuracy: {}'.format(accuracy_score(hold_out_y, pred_y)))
            #print(classification_report(hold_out_y, pred_y))


    if privacy == 'obj_pert':
        noise = dict()
        n = len(train_x)
        epsilon2 = epsilon - 2 * np.log(1 + 1. / (4 * n * l2_ratio))
        if epsilon2 > 0:
            Delta = 0.
        else:
            Delta = 1. / (4 * n * (np.exp(epsilon / 4.) - 1)) - l2_ratio
            epsilon2 = epsilon / 2.    
        for param in params:
            value = param.get_value(borrow=True)
            noise[param] = np.array(np.random.laplace(0, 2. / epsilon2, value.shape), dtype=value.dtype) / n            
        loss += Delta * lasagne.regularization.regularize_network_params(output_layer, lasagne.regularization.l2)
        updates = lasagne.updates.adam(loss, params, learning_rate=learning_rate)
        updates = perturb(updates, params, learning_rate, noise=noise)

    elif privacy == 'grad_pert':
        n = len(train_x)
        alpha = 2 * np.log(1 / delta) / epsilon + 1 # Parameter for Renyi Divergence
        C = 1 # Clipping Threshold
        _q = batch_size / n # Sampling Ratio
        _T = epochs * n / batch_size # Number of Steps
        sigma = 0.
        if dp == 'adv_cmp':
            sigma = np.sqrt(2 * epochs * np.log(2.5 * epochs / delta)) * (np.sqrt(np.log(2 / delta) + 2 * epsilon) + np.sqrt(np.log(2 / delta))) / epsilon # Adv Comp
        elif dp == 'zcdp':
            sigma = np.sqrt(epochs / 2) * (np.sqrt(np.log(1 / delta) + epsilon) + np.sqrt(np.log(1 / delta))) / epsilon # zCDP
        elif dp == 'rdp':
            sigma = _q * np.sqrt(_T * (2 * np.log(1 / delta) + epsilon)) / epsilon # RDP --run using rdp_accountant?
        elif dp == 'dp':
            sigma = epochs * np.sqrt(2 * np.log(1.25 * epochs / delta)) / epsilon # DP
        print(sigma)
        updates = perturb(updates, params, learning_rate, sigma=sigma, C=epsilon)

    train_fn = theano.function([input_var, target_var], loss, updates=updates, allow_input_downcast=True)
    test_prediction = lasagne.layers.get_output(output_layer, deterministic=True)
    test_fn = theano.function([input_var], test_prediction, allow_input_downcast=True)

    #print('Training...')
    train_loss = 0
    for epoch in range(epochs):
        loss_ = 0
        for input_batch, target_batch in iterate_minibatches(train_x, train_y, batch_size):
            loss_ += train_fn(input_batch, target_batch)
        train_loss = loss_
        loss_ = round(loss_, 3)
        if not silent:
            print('Epoch {}, train loss {}'.format(epoch, loss_))

    if privacy == 'out_pert':
        n = len(train_x)
        final_params = lasagne.layers.get_all_param_values(output_layer)
        for param in final_params:
            param += np.array(np.random.laplace(0, 2. / (l2_ratio * epsilon), param.shape), dtype=param.dtype) / n   
        lasagne.layers.set_all_param_values(output_layer, final_params)

    pred_y = []
    for input_batch, _ in iterate_minibatches(train_x, train_y, batch_size, shuffle=False):
        pred = test_fn(input_batch)
        pred_y.append(np.argmax(pred, axis=1))
    pred_y = np.concatenate(pred_y)

    train_acc = accuracy_score(train_y, pred_y)

    if not silent:
        print('Training Accuracy: {}'.format(accuracy_score(train_y, pred_y)))
        #print(classification_report(train_y, pred_y))

    if test_x is not None:
        #print('Testing...')
        pred_y = []
        pred_scores = []

        if batch_size > len(test_y):
            batch_size = len(test_y)

        for input_batch, _ in iterate_minibatches(test_x, test_y, batch_size, shuffle=False):
            pred = test_fn(input_batch)
            pred_y.append(np.argmax(pred, axis=1))
            pred_scores.append(pred)
        pred_y = np.concatenate(pred_y)
        pred_scores = np.concatenate(pred_scores)
        test_acc = accuracy_score(test_y, pred_y)
        if not silent:
            print('Testing Accuracy: {}'.format(accuracy_score(test_y, pred_y)))
            #print(classification_report(test_y, pred_y))

        return output_layer, pred_y, pred_scores, train_loss, train_acc, test_acc


def load_dataset(train_feat, train_label, test_feat=None, test_label=None):
    train_x = np.genfromtxt(train_feat, delimiter=',', dtype='float32')
    train_y = np.genfromtxt(train_label, dtype='int32')
    min_y = np.min(train_y)
    train_y -= min_y
    if test_feat is not None and test_label is not None:
        test_x = np.genfromtxt(train_feat, delimiter=',', dtype='float32')
        test_y = np.genfromtxt(train_label, dtype='int32')
        test_y -= min_y
    else:
        test_x = None
        test_y = None
    return train_x, train_y, test_x, test_y


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('train_feat', type=str)
    parser.add_argument('train_label', type=str)
    parser.add_argument('--test_feat', type=str, default=None)
    parser.add_argument('--test_label', type=str, default=None)
    parser.add_argument('--model', type=str, default='nn')
    parser.add_argument('--learning_rate', type=float, default=0.01)
    parser.add_argument('--batch_size', type=int, default=100)
    parser.add_argument('--n_hidden', type=int, default=50)
    parser.add_argument('--epochs', type=int, default=100)
    args = parser.parse_args()
    print(vars(args))
    dataset = load_dataset(args.train_feat, args.train_label, args.test_feat, args.train_label)
    train(dataset,
          model=args.model,
          learning_rate=args.learning_rate,
          batch_size=args.batch_size,
          n_hidden=args.n_hidden,
          epochs=args.epochs)


if __name__ == '__main__':
    main()
