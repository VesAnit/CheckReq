import numpy
print('numpy:',numpy.__version__)
import pandas
print('pandas:',pandas.__version__)
import sklearn
print('scikit-learn:',sklearn.__version__)
import matplotlib
print('matplotlib:',matplotlib.__version__)
import seaborn
print('seaborn:',seaborn.__version__)
import scipy
print('scipy:',scipy.__version__)
import torch
print('torch:',torch.__version__)
print('CUDA available:',torch.cuda.is_available())
import torchvision
print('torchvision:',torchvision.__version__)
import torchaudio
print('torchaudio:',torchaudio.__version__)
import tensorflow
print('tensorflow:',tensorflow.__version__)
print('TF GPU devices:',len(tensorflow.config.list_physical_devices('GPU')))
import subprocess
print(subprocess.run(['pip', 'check'], capture_output=True, text=True).stdout)