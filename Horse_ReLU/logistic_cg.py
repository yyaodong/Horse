#-*- encoding:utf-8 -*-

__docformat__ = 'restructedtext en'


import os
import sys
import time
import numpy
import theano
import theano.tensor as T
from scipy.stats import itemfreq
import scipy.optimize


def load_data(trainset, validset, testset):

    #分别读入三个文件并share他们
    data=numpy.loadtxt(trainset, delimiter=',', dtype=float, skiprows=1)
    train_set=(data[:,:-2],data[:,-2],data[:,-1])

    data = numpy.loadtxt(validset, delimiter=',', dtype=float, skiprows=1)
    valid_set=(data[:,:-2],data[:,-2],data[:,-1]) #feature,label,raceid

    data=numpy.loadtxt(testset, delimiter=',', dtype=float, skiprows=1)
    test_set=(data[:,:-2],data[:,-2],data[:,-1])

    def shared_dataset(data_xy, borrow=True):

        data_x, data_y, data_index = data_xy
        shared_x = theano.shared(numpy.asarray(data_x,
                                               dtype=theano.config.floatX),
                                 borrow=borrow)
        shared_y = theano.shared(numpy.asarray(data_y,
                                               dtype=theano.config.floatX),
                                 borrow=borrow)
        # index实际的样子是从0开始每一位记录每组赛事的第一名的位置，也就是每组比赛开始的地方，最后一位是全部输入sample的行数
        data_index = numpy.concatenate((numpy.array([0]), numpy.cumsum(itemfreq(data_index)[:,1])))

        shared_index = theano.shared(numpy.asarray(data_index,
                                               dtype=theano.config.floatX),
                                 borrow=borrow)

        return shared_x, T.cast(shared_y, 'int32'), T.cast(shared_index, 'int32')

    #主要是用x和index，y未来再使用
    test_set_x, test_set_y, test_set_index = shared_dataset(test_set)
    valid_set_x, valid_set_y, valid_set_index = shared_dataset(valid_set)
    train_set_x, train_set_y, train_set_index = shared_dataset(train_set)

    rval = [(train_set_x, train_set_y, train_set_index), (valid_set_x, valid_set_y, valid_set_index),
            (test_set_x, test_set_y, test_set_index)]
    return rval

class ConditionalLogisticRegression(object):

    def __init__(self, input, n_in, index, theta = None, W=None, b=None): #input是一个minibatch，单位是一组赛事，不是一个sample


        n_out=1  #对于CL模型来说，并不是每一类构建一个分类平面，一直都只有一个数值,就是每匹马夺冠的概率

        #把W和b写在theta里面方便T.grad

        if theta is None:
            self.theta = theano.shared(
                value=numpy.zeros(
                n_in * n_out + n_out,
                dtype=theano.config.floatX
                #dtype='float32'
                ),
                name='theta',
                borrow=True
            )
        else:
            self.theta = theta

        _W = self.theta[0:n_in * n_out].reshape((n_in, n_out))
        _b = self.theta[n_in * n_out:n_in * n_out + n_out]

        if W is None:
            self.W = _W
            self.b = _b
        else:
            self.W = W
            self.b = b

        # 把线性回归的值exp之后再按组归一化就是最后的值
        _raw_w = T.exp(T.dot(input, self.W) + self.b)

        # 计算每组比赛内的exp和
        def cumsum_within_group(_start, _index, _race):
            start_point = _index[_start]
            stop_point = _index[_start+1]
            return T.sum(_race[start_point:stop_point], dtype='float32')

        # _cumsum就是每组的exp的合
        _cumsum, _ = theano.scan(cumsum_within_group,
                                 sequences=[T.arange(index.shape[0]-1)],
                                 non_sequences=[index, _raw_w])


        #构造一个rep(cumsum,times)的序列，目的是直接相除从而得到每匹马的概率
        # _times里存的是每组比赛的马的数量
        self._times, _ = theano.scan(fn=lambda i, index: index[i+1]-index[i],
                                     sequences=[T.arange(index.shape[0]-1)],
                                     non_sequences=index)

        _raceprobdiv = T.ones_like(_raw_w)

        # 这里运用的技巧是构造一个等长的序列，然后用T.set_subtensor改变里面的值，SCAN不允许每次输出长度不一样的序列，所以不可以concatenate
        def change_race_prob_div(_i, _change, _rep, _times, _item):
            _change = T.set_subtensor(_change[_rep[_i]:_rep[_i+1]], T.reshape(T.alloc(_item[_i],_times[_i]),(_times[_i],1)))
            return _change

        # _race_prob_div存的是每一位对应的要除的概率归一化的值
        _race_prob_div, _ = theano.scan(fn = change_race_prob_div,
                                        sequences=[T.arange(index.shape[0]-1)],
                                        outputs_info=[_raceprobdiv],
                                        non_sequences=[index,self._times, _cumsum])

        #归一化以后的概率值,整个init过程最重要的就是计算每匹马的概率，在普通的logistic里计算这个不需要label,只要一个softmax就行
        self.race_prob = _raw_w / _race_prob_div[-1]

        self.mean_neg_loglikelihood = None

        self.neg_log_likelihood = None

        self.pos_log_likelihood=None

        self.r_square = None

        self.r_error = None

        self.params = [self.W, self.b]

    def negative_log_likelihood(self, index):

        #特别注意：output_info一定不能用numpy组成的序列，用也要禁掉broadcast，或者干脆用shared variable
        _output_info = T.as_tensor_variable(numpy.array([0.]))

        _output_info = T.unbroadcast(_output_info, 0)

        # _1st_prob存的是对每次比赛第一匹马的likelihood求和的过程
        _1st_prob, _ = theano.scan(fn= lambda _1st, prior_reuslt, _prob: prior_reuslt+T.log(_prob[_1st]),
                                   sequences=[index[:-1]],
                                   outputs_info=_output_info,
                                   non_sequences=self.race_prob)

        self.neg_log_likelihood = 0. - _1st_prob[-1] #这个是负的

        self.pos_log_likelihood = _1st_prob[-1]

        self.mean_neg_loglikelihood = self.neg_log_likelihood/(index.shape[0]-1)

        #因为cost必须是0维的，所以用T.mean巧妙的转换一下
        return T.mean(self.mean_neg_loglikelihood.ravel(), dtype='float32')

    def Rsquare(self, index): #rsqaure约大越好，函数返回的值越小越好

        _output_info = T.as_tensor_variable(numpy.array([0.]))

        _output_info = T.unbroadcast(_output_info, 0)

        # rsquare计算是除以Ln(1/n_i),n_i是每组比赛中马的个数
        _r_square_div, _ = theano.scan(fn = lambda _t, prior_reuslt: prior_reuslt+T.log(1./_t),
                                       sequences=[self._times],
                                       outputs_info=_output_info #特别注意：output_info一定不能用numpy组成的序列，用shared或者禁掉broadcast
                                       )

        self.r_error = self.pos_log_likelihood / _r_square_div[-1]

        self.r_square = 1 - self.r_error

        #用T.mean转化成一维的
        return T.mean(self.r_error.ravel(), dtype='float32')

    def show_theta(self):

        return self.theta.get_value()

def cg_optimization_horse(dataset, n_epochs=50, batch_size=100, validating_mode='all', optimization='BFGS'):

    #############
    # LOAD DATA #
    #############
    datasets = load_data(dataset[0], dataset[1], dataset[2])

    train_set_x, train_set_y, train_set_index = datasets[0]
    valid_set_x, valid_set_y, valid_set_index = datasets[1]
    test_set_x, test_set_y, test_set_index = datasets[2]

    batch_size = batch_size    # size of the minibatch

    #-1是因为多一个零，因为index第一位必须标明第一组比赛起始的地方
    n_train_batches = (len(numpy.unique(train_set_index.eval()))-1) / batch_size
    n_valid_batches = (len(numpy.unique(valid_set_index.eval()))-1) / batch_size
    n_test_batches = (len(numpy.unique(test_set_index.eval()))-1) / batch_size

    n_in = train_set_x.shape[1].eval()  # number of features in a horse
    n_out = 1
    ######################
    # BUILD ACTUAL MODEL #
    ######################
    print '... building the model'

    minibatch = T.lscalar()
    x = T.matrix()
    index = T.ivector()

    #用symbol构建出model的类
    classifier = ConditionalLogisticRegression(input=x, n_in=n_in, index=index)
    cost = classifier.negative_log_likelihood(index)

    #根据一个Minibatch号码，提供一个batchsize多组的sample, 在test数据上计算模型的rsquare，最终的rsquare是batch上rsquare的平均
    if validating_mode == 'batch':

        test_model = theano.function(
            [minibatch],
            classifier.Rsquare(index), #计算test set上的错误率
            givens={
                x: test_set_x[test_set_index[minibatch]:test_set_index[minibatch + batch_size]],  #因为寻址最后一位找不到
                index: test_set_index[minibatch:(minibatch + batch_size + 1)] - test_set_index[minibatch]
            },
            name="test",
            allow_input_downcast=True
        )
    #根据一个Minibatch号码，提供一个batchsize多组的sample, 在valid数据上计算模型的rsquare，最终的rsquare是batch上rsquare的平均
        validate_model = theano.function(
            [minibatch],
            classifier.Rsquare(index), #计算validate set上的错误率 R error
            givens={
                x: valid_set_x[valid_set_index[minibatch]:valid_set_index[minibatch + batch_size]],
                index: valid_set_index[minibatch:(minibatch + batch_size + 1)] - valid_set_index[minibatch]
            },
            name="validate",
            allow_input_downcast=True
        )

        train_model = theano.function(
            [minibatch],
            classifier.Rsquare(index), #计算validate set上的错误率 R error
            givens={
                x: train_set_x[train_set_index[minibatch]:train_set_index[minibatch + batch_size]],
                index: train_set_index[minibatch:(minibatch + batch_size + 1)] - train_set_index[minibatch]
            },
            name="train",
            allow_input_downcast=True
        )

    elif validating_mode == 'all': #用全部样本计算R2，而不是算每个BATCH然后再平均

        train_model = theano.function(
            [],
            classifier.Rsquare(index),
            givens={
                x: train_set_x,
                index: train_set_index
            },
            name='train',
            allow_input_downcast=True
        )

        test_model = theano.function(
            [],
            classifier.Rsquare(index), #计算test set上的错误率
            givens={
                x: test_set_x,  #因为寻址最后一位找不到
                index: test_set_index
            },
            name="test",
            allow_input_downcast=True
        )

        validate_model = theano.function(
            [],
            classifier.Rsquare(index), #计算validate set上的错误率 R error
            givens={
                x: valid_set_x,
                index: valid_set_index
            },
            name="validate",
            allow_input_downcast=True
        )


    #提供train_set上从Minibatch开始接下去一个batchsize这么多组的赛马sample，计算model在这些比赛上likelihood
    batch_cost = theano.function(
        [minibatch],
        cost, #计算一个起始点开始接下来batch_size个函数的log-liklihood
        givens={
            x: train_set_x[train_set_index[minibatch]:train_set_index[minibatch + batch_size]],
            index: train_set_index[minibatch:(minibatch + batch_size + 1)] - train_set_index[minibatch]
        },
        name="batch_cost",
        allow_input_downcast=True
    )

    #根据上面提供的cost计算gradient
    batch_grad = theano.function(
        [minibatch],
        T.grad(cost, classifier.theta),
        givens={
            x: train_set_x[train_set_index[minibatch]:train_set_index[minibatch + batch_size]],
            index: train_set_index[minibatch:(minibatch + batch_size + 1)] - train_set_index[minibatch]
        },
        name="batch_grad",
        allow_input_downcast=True
    )

    # 计算train_set数据上的cost, loop所有的batch，最后cost按平均值算
    def train_fn(theta_value):
        classifier.theta.set_value(numpy.float32(theta_value), borrow=True)
        train_losses = [batch_cost(i * batch_size)
                        for i in xrange(n_train_batches)] #在所有的train batch上计算cost，然后输出均值
        return numpy.mean(train_losses)

    # 计算在train_Set的所有batch上的gradient,然后做平均
    def train_fn_grad(theta_value):
        classifier.theta.set_value(numpy.float32(theta_value), borrow=True)
        grad = batch_grad(0)
        for i in xrange(1, n_train_batches): #在train batch上累加gradient然后除以batch的个数
            grad += batch_grad(i * batch_size)
        return grad / n_train_batches

    validation_scores = [numpy.inf, 0, 0] #用来记录在validation和test上的最完美的rsquare
    filename = ["eph", str(n_epochs), "_bs", str(batch_size), '_best.csv']
    best_weights = [None,0] #要存call_back里返回的函数，一定要用python自带的list,不然返回会失败
    # 在train_set的每个batch的赛事输入以后，计算当前在train_set上的cost和下一步的gradient
    # 然后计算该模型在validation数据上的rsquare，如果创了记录，那么就在test数据上测试
    def callback(theta_value):
        classifier.theta.set_value(numpy.float32(theta_value), borrow=True)

        if validating_mode=='batch':
            validation_losses = [validate_model(i * batch_size) #计算valid_set上的r suqare, 平均每个batch
                                for i in xrange(n_valid_batches)]
            this_validation_loss = numpy.mean(validation_losses)
            train_losses = [train_model(i * batch_size) #计算valid_set上的r suqare, 平均每个batch
                                for i in xrange(n_train_batches)]
            this_train_loss = numpy.mean(train_losses)
        elif validating_mode=='all':
            this_validation_loss = validate_model()
            this_train_loss = train_model()
        print('validation R Square %f , train R square %f ' % (1-this_validation_loss, 1-this_train_loss))

        if this_validation_loss < validation_scores[0]:

            validation_scores[0] = this_validation_loss
            validation_scores[2] = this_train_loss
            #model最好的时候存权重
            best_weights[0] = classifier.show_theta() #get_value之后中间不需要function再过度
            #print classifier.theta.eval()

            #暂时关闭在test set上测试R2的功能，因为test和valid一样，是一份数据
            # if validating_mode=='batch':
            #     test_losses = [test_model(i * batch_size) #如果效果好就在test set上计算rsquare
            #                 for i in xrange(n_test_batches)]
            #     validation_scores[1] = numpy.mean(test_losses)
            #
            # elif validating_mode=='all':
            #     validation_scores[1] = test_model()

    ###############
    # TRAIN MODEL #
    ###############

    # using scipy conjugate gradient optimizer
    if optimization=='LBFGS':

        print ("Optimizing using scipy.optimize.fmin_l_bfgs...")
        start_time = time.clock()
        #best_w_b是在train data上表现最好的参数
        best_w_b = scipy.optimize.fmin_l_bfgs_b(
            func=train_fn,#存cost
            x0=numpy.zeros((n_in + 1) * n_out, dtype=x.dtype),
            fprime=train_fn_grad, #存gradient
            callback=callback,#在train_set上每train一个minibatch后就测试在valid_set上的r2，存一个最好的，测试函数就是这里的callback
            disp=0,
            maxiter=n_epochs
            #,full_output=True
        )

    elif optimization=='BFGS':

        print ("Optimizing using scipy.optimize.fmin_bfgs...")
        start_time = time.clock()
        #best_w_b是在train data上表现最好的参数
        best_w_b = scipy.optimize.fmin_bfgs(
            f=train_fn,#存cost
            x0=numpy.zeros((n_in + 1) * n_out, dtype=x.dtype),
            fprime=train_fn_grad, #存gradient
            callback=callback,#在train_set上每train一个minibatch后就测试在valid_set上的r2，存一个最好的，测试函数就是这里的callback
            disp=0,
            maxiter=n_epochs,
            full_output=True
        )

    #train上最好的params在best_w_b[0]
    #不能输出R2,1 loglikelihood是平均值，除过batchsize, 2 不知道每个batch里面有多少马

    print(
        (
            'Optimization complete best R2 Validating data %f , with '
            'training performance %f '
        )
        % (1-validation_scores[0] , 1-validation_scores[2] )
    )

    #每次call back存最好的那组结果，但是在函数最外面再出书那个结果，提升速度
    numpy.savetxt("".join(filename), numpy.hstack((best_weights[0], 1-validation_scores[0])), delimiter=',')

    #关闭功能：存train data上的最好结果的权重
    #filename = ["eph", str(n_epochs), "_bs", str(batch_size), '_best_Train.csv']
    #numpy.savetxt("".join(filename), best_w_b[0], delimiter=',')

    end_time = time.clock()
    print >> sys.stderr, ('The code for file ' + os.path.split(__file__)[1] + ' ran for %.1fs' % ((end_time - start_time)) +
                            ', with batch_size ' + str(batch_size) + ', best R2 on training %f'
                            ', on validation %f ' % (1-validation_scores[2] , 1-validation_scores[0] ))


if __name__ == '__main__':
    #
    # delta = 20
    # end_point = 12760
    # n_job=11
    # _t=(end_point-delta)/delta
    # sep = numpy.linspace(1,_t,n_job+1).astype(int)
    # sep_delta = (_t-1)/(n_job)
    #
    # if sys.argv[1]=='1':
    #      for i in xrange( sep[int(sys.argv[1])-1]*delta, (sep[int(sys.argv[1])-1]+sep_delta)*delta, delta):
    #         cg_optimization_horse(n_epochs=150, batch_size=i, dataset=['horse_train.csv','horse_valid.csv','horse_test.csv'],
    #                             validating_mode='all')
    # elif sys.argv[1]=='2':
    #     for i in xrange( sep[int(sys.argv[1])-1]*delta, (sep[int(sys.argv[1])-1]+sep_delta)*delta, delta):
    #         cg_optimization_horse(n_epochs=150, batch_size=i, dataset=['horse_train.csv','horse_valid.csv','horse_test.csv'],
    #                             validating_mode='all')
    # elif sys.argv[1]=='3':
    #     for i in xrange( sep[int(sys.argv[1])-1]*delta, (sep[int(sys.argv[1])-1]+sep_delta)*delta, delta):
    #         cg_optimization_horse(n_epochs=150, batch_size=i, dataset=['horse_train.csv','horse_valid.csv','horse_test.csv'],
    #                             validating_mode='all')
    # elif sys.argv[1]=='4':
    #     for i in xrange( sep[int(sys.argv[1])-1]*delta, (sep[int(sys.argv[1])-1]+sep_delta)*delta, delta):
    #         cg_optimization_horse(n_epochs=150, batch_size=i, dataset=['horse_train.csv','horse_valid.csv','horse_test.csv'],
    #                             validating_mode='all')
    # elif sys.argv[1]=='5':
    #     for i in xrange( sep[int(sys.argv[1])-1]*delta, (sep[int(sys.argv[1])-1]+sep_delta)*delta, delta):
    #         cg_optimization_horse(n_epochs=150, batch_size=i, dataset=['horse_train.csv','horse_valid.csv','horse_test.csv'],
    #                             validating_mode='all')
    # elif sys.argv[1]=='6':
    #     for i in xrange( sep[int(sys.argv[1])-1]*delta, (sep[int(sys.argv[1])-1]+sep_delta)*delta, delta):
    #         cg_optimization_horse(n_epochs=150, batch_size=i, dataset=['horse_train.csv','horse_valid.csv','horse_test.csv'],
    #                             validating_mode='all')
    # elif sys.argv[1]=='7':
    #     for i in xrange( sep[int(sys.argv[1])-1]*delta, (sep[int(sys.argv[1])-1]+sep_delta)*delta, delta):
    #         cg_optimization_horse(n_epochs=150, batch_size=i, dataset=['horse_train.csv','horse_valid.csv','horse_test.csv'],
    #                             validating_mode='all')
    # elif sys.argv[1]=='8':
    #     for i in xrange( sep[int(sys.argv[1])-1]*delta, (sep[int(sys.argv[1])-1]+sep_delta)*delta, delta):
    #         cg_optimization_horse(n_epochs=150, batch_size=i, dataset=['horse_train.csv','horse_valid.csv','horse_test.csv'],
    #                             validating_mode='all')
    # elif sys.argv[1]=='9':
    #     for i in xrange( sep[int(sys.argv[1])-1]*delta, (sep[int(sys.argv[1])-1]+sep_delta)*delta, delta):
    #         cg_optimization_horse(n_epochs=150, batch_size=i, dataset=['horse_train.csv','horse_valid.csv','horse_test.csv'],
    #                             validating_mode='all')
    #         print i
    # elif sys.argv[1]=='10':
    #     for i in xrange( sep[int(sys.argv[1])-1]*delta, (sep[int(sys.argv[1])-1]+sep_delta)*delta, delta):
    #         cg_optimization_horse(n_epochs=150, batch_size=i, dataset=['horse_train.csv','horse_valid.csv','horse_test.csv'],
    #                             validating_mode='all')
    #         print i
    # elif sys.argv[1]=='11':
    #     for i in xrange( sep[int(sys.argv[1])-1]*delta, (sep[int(sys.argv[1])-1]+sep_delta)*delta, delta):
    #         cg_optimization_horse(n_epochs=150, batch_size=i, dataset=['horse_train.csv','horse_valid.csv','horse_test.csv'],
    #                             validating_mode='all')

    cg_optimization_horse(n_epochs=400, batch_size=1, dataset=['horse_train.csv','horse_valid.csv','horse_test.csv'],
                          validating_mode='all',optimization='BFGS')