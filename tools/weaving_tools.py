from tqdm.notebook import tqdm
from pathlib import Path
from PIL import Image
from datasets import Dataset, concatenate_datasets
from sentence_transformers import SentenceTransformer #, util
from transformers import  AutoModel, AutoFeatureExtractor #, AutoTokenizer
from tensorboard.plugins import projector
#from transformers import CLIPProcessor, CLIPModel, CLIPImageProcessor, CLIPTokenizer
from lxml import etree
from typing import Union
import pandas as pd
import numpy as np
import tensorflow as tf
import json
import os
import PIL
import torch
import torchvision.transforms as T
import matplotlib.pyplot as plt
import requests
import random
import time



# ----------------------------------
# -------- Helper functions --------
# ----------------------------------

def lower_case(examples, target_col='description'):
    return {'text': [t.lower() for t in examples[target_col]]}

def open_image(record,target_col='img_path'):
  return {'image' : Image.open(record[target_col])}


def plot_images(query_df):
        """plot nearest neighbour images"""
        fig = plt.figure(figsize=(20, 6))
        columns = 3
        rows = 3
        for i in range(1, columns*rows +1):
            
            img = Image.open(query_df.loc[i-1,'img_path'])
            fig.add_subplot(rows, columns, i)
            plt.imshow(img)
        plt.show()



# ----------------------------------
# --- Generic Collection Class -----
# ----------------------------------

class MultiModalCollection(object):
    """Generic Collection class that provides most of 
    functionalities and tools we want to apply to the 
    multimodal online catalogues.
    """
    def __init__(self,df=None, img_folder: str='imgs',device: str='cpu'):
        self.df = df
        self.img_folder = Path(img_folder)
        self.img_folder.mkdir(exist_ok=True)
        self.images = set(self.img_folder.glob('*.jpg'))   
        self.device = device if torch.backends.mps.is_available() else "cpu"

    def load_from_csv(self, path_to_csv: str):
        self.df = pd.read_csv(path_to_csv, index_col=0)
    
    def filter_records(self):
        """removes all records without images
        automatically adapts the dataset as well
        """
        self.df = self.df[(self.df.downloaded==True) & (~self.df['description'].isnull())].reset_index()
        self.to_dataset()


    def save_csv(self,out_path):
        self.df.to_csv(out_path)

    def to_dataset(self):
        """convert a pandas dataframe to a hugging face dataset instance"""
        self.dataset = Dataset.from_pandas(self.df)
    
    def __len__(self):
        return self.df.shape[0]
    
    def __str__(self):
        return f'< catalogue with {self.df.shape[0]} records >'

    def extract_clip_embedding(self,record: dict, modality: str) -> dict:
        """create clip embedding"""
        return {f'clip_{modality}_embedding':self.clip_model.encode(record[modality])#.detach().cpu().numpy()
                }

    def load_clip_model(self,clip_model_ckpt: str='clip-ViT-B-32'):
        """load clip model and convert with sentence transformer"""
        self.clip_model_ckpt = clip_model_ckpt
        self.clip_model = SentenceTransformer(self.clip_model_ckpt)
        self.clip_model.to(self.device)

    def embed_clip(self,
                   target_col: str,
                   modality: str,
                   model_ckpt: str="clip-ViT-B-32"):
        """embed and text or image with clip
        Arguments:
            target_col (str): which columns to embed
            modality (str): either 'text' or 'image'
            model_ckpt (str): checkpoint of clip model to use
        """
        
        if not hasattr(self,'clip_model'):
            self.load_clip_model(model_ckpt)

        if modality == 'text':
            self.dataset = self.dataset.map(lower_case, fn_kwargs= {'target_col': target_col})
            

        elif modality == 'image':
            self.dataset = self.dataset.map(open_image, fn_kwargs={'target_col': target_col})
            
        else:
            raise Exception("Modality has to be either 'text' or 'image")

        self.dataset = self.dataset.map(self.extract_clip_embedding, fn_kwargs= {'modality': modality})

    def vectorize_collection(self, clip_ckpt: str='clip-ViT-B-32'):
        """vectorize the collection"""
        self.filter_records()

        for target_col, modality in [('img_path','image'),('description','text')]:
            print(f'Vectorizing {modality}')
            self.embed_clip(target_col,modality,clip_ckpt)

    def add_embeddings_to_database(self, collection, modality):
        
        collection.add(
                embeddings = [list(v) for v in self.dataset[f'clip_{modality}_embedding']],
                documents=list([str(t) for t in self.dataset['description']]),
                metadatas=[{"collection": self.collection_name,
                            'modality': modality, 
                            'img_path': row.img_path, 
                            'record_id': row.record_id
                                } for i, row in self.df.iterrows()],
            ids = [f'{row.record_id}_{modality}_{i}' for i, row in self.df.iterrows()] 
        )


# ----------------------------------
# ----- SMG Collection Class -------
# ----------------------------------


class SMGCollection(MultiModalCollection):
    """Main object for processing data from the
    Science Museum Group online catalogue. 
    This is a subclass of the generic Collection
    class that contains most of the functionality
    that should apply to all collections.
    """

    def __init__(self, df: pd.DataFrame = pd.DataFrame(), img_folder: str='smg_imgs', device: str='cpu'):
        # self.df = df
        # self.img_folder = Path(img_folder)
        # self.img_folder.mkdir(exist_ok=True)
        # self.images = set(self.img_folder.glob('*.jpg'))   
        # self.indices = dict()
        # self.device = device if torch.backends.mps.is_available() else "cpu"
        MultiModalCollection.__init__(self,df,img_folder,device)
        self.collection_name = 'smg'
   
    def load_from_json(self, path_to_json: str):
        """load the collection from a json file which
        contains an output of the original database

        Argument:
            path_to_json (str): path to the json dump
        """
        data = []
        with open(path_to_json,'r') as in_json:
            # iterate over all the dictionaries
            for d in tqdm(in_json):
                raw_record = json.loads(d) 
                processed_record = self.process_json_record(raw_record)
                data.append(processed_record)
        self.df = pd.DataFrame(
                        data, 
                        columns=['record_id','name','description','taxonomy','img_loc','img_name','img_path','downloaded']
                        )
        # replace all nans with white space
        self.df.fillna('', inplace=True)

    def process_json_record(self,record: dict) -> list:
        """Principal function for processing records in the json dump
        of the smg collection. Here we collection the most important pieces
        of information we want to use later for our experiments (either
        the multimodal analysis of )

        Arguments:
            record (dict)

        Returns:
            list with the following elements
                record_id (str): the original id as recorded by the SMG database
                names (str): names of the object, concatenated with a semicolon
                description (str): all descriptions, concatenated with a semicolon
                taxonomy (str): taxonomy terms as string but sorted according to the hierarchy
                img_loc (str): location of the medium sized image
                img_name (str): formated the name of the images
                img_path (str): path to the images
                downloaded (bool): flag indicating whether we downloaded the image
        """
        
        record_id = record['_id']
        source =  record['_source'] # get the source element
        # get all the description under the description attribute
        description = '; '.join([s.get('value','').strip() for s in source.get('description',[])])
        # whitespaces seems to split rows when saving csv
        description = ' '.join(description.split()) 
        # get all the the names under the name attribute
        names =  '; '.join([s.get('value','').strip() for s in source.get('name',[])])
        # whitespaces seems to split rows when saving csv
        names = ' '.join(description.split())
        # get all the taxonmy terms
        terms =  source.get('terms',None)
        taxonomy = ''
        if terms:
            # map all the taxonomy from sort order in the hierarchy to their name
            taxonomy = {
                        t['sort']: t['name'][0]['value'] 
                            for t in terms[0].get('hierarchy',[])
                                }
            # convert all taxonomy terms to a string in sorted order
            taxonomy = '; '.join([v.strip() for k,v in sorted(taxonomy.items()) 
                                        #if not v.startswith('<') # optional, skip terms starting with <
                                                ])
        
        img_loc, img_name, img_path = '', '', ''
        multimedia = source.get('multimedia',None)
        if multimedia:
            # get the medium file size
            img_loc =  multimedia[0]['processed']['medium']['location']
            # reformat image file name, so it correspond to local path
            # we in fetch_images replaced the forward slash with a |
            img_name = img_loc.replace('/','|')
            img_path = self.img_folder / img_name
        
        downloaded = img_path in self.images
        
        return [record_id,names,description, taxonomy, img_loc ,img_name, img_path, downloaded]
    
    def fetch_images(self, max_images: int=100) -> None:
        """Given a json dump with all records fetch
        and save images in a img_folder

        Arguments:
            max_images (int): number of images to download
        """

        def fetch_image(loc: str) -> bool:   
            """
            scrape an image by name as provided in the json file

            Arguments:
                loc (str): name of the image
            """
            url = base_url + '/'+ loc
            img_name = loc.replace('/','|')
            request  = requests.get(url)
            
            if request.status_code == 200: # check if request is successful    
                with open(self.img_folder / img_name, 'wb') as f:
                    f.write(request.content)
                    time.sleep(random.uniform(.25, .25)) # randomize the requests
                    return True
            return False

        print('before downloading',len(self.images)) 
        # get all the rows for images that are not downloaded yet and take a subset of `max_images` 
        img_locs_all = list(self.df[(self.df.downloaded==False) & (self.df.img_loc!= '')].img_loc)
        print('remaining images to download', len(img_locs_all))
        img_locs = img_locs_all[:max_images]
        
        # download the images
        # hard coded base url for getting images from the SMG group
        base_url = 'https://coimages.sciencemuseumgroup.org.uk/images'
        _ = [fetch_image(r) for r in tqdm(img_locs)]
        # get the number of downloaded images
        self.images = set(self.img_folder.glob('*.*')) 
        print('after downloading',len(self.images))
  

# ----------------------------------
# ----- BT Collection Class -------
# ----------------------------------

class BTCollection(MultiModalCollection):
    
    def __init__(self,df=None, img_folder='imgs',device='cpu'):
        MultiModalCollection.__init__(self,df,img_folder,device)

    def fetch_images(self, n=-1):
        def fetch_image(loc: str):   
            url = base_url + '/'+ loc
            img_name = loc.split('/')[-1]
            request  = requests.get(url)
            if request.status_code == 200:
                
                with open(self.img_folder / img_name, 'wb') as f:
                    f.write(request.content)
                    time.sleep(random.uniform(.5, 1.5))
                    return True
            return False
        
        img_names = list(self.df[~self.df.Thumbnail.isnull()].Thumbnail)
        img_names =  [img for img in img_names if not (self.img_folder / img).is_file()][:n]
 
        base_url = 'http://www.digitalarchives.bt.com/CalmView/GetImage.ashx?db=Catalog&type=default&fname='
        for img in tqdm(img_names):
            fetch_image(img)


    def load_from_xml(self,path):
        def find_and_get_text(record, element):
            result = record.find(element)
            if result is not None:
                return result.text
            return ''
        
        with open(path, 'rb') as in_xml:
            tree = etree.parse(in_xml)
        records = tree.xpath('//DScribeRecord')
        data = []
        columns = ['RefNo','Title','Thumbnail','Description']
        for r in tqdm(records):
            data.append([find_and_get_text(r,c) for c in columns])
        
        self.df = pd.DataFrame(data, columns=columns)
        self.df['img_path'] = self.df.Thumbnail.apply(lambda x: self.img_folder / x if x else x)
        self.df['downloaded'] = self.df.img_path.apply(lambda x: Path(x).is_file() if x else False)
        self.df.rename({'RefNo':'record_id','Title':'names','Thumbnail':'img_loc','Description':'description'},
                       axis=1, inplace=True)

          

# ----------------------------------
# ----- NMS Collection Class -------
# ----------------------------------

class NMSCollection(MultiModalCollection):
    def __init__(self,df=None, img_folder='nms_imgs',device='cpu'):
        MultiModalCollection.__init__(self,df,img_folder,device)
        self.collection_name = 'nms'

    def load_original_csvs(self,files):
        """Read the original CVS files containing the NMS collection
        Turns this into a combined csv with the following fields

                record_id (str): the original id as recorded by the SMG database
                names (str): names of the object, concatenated with a semicolon
                description (str): all descriptions, concatenated with a semicolon
                taxonomy (str): taxonomy terms as string but sorted according to the hierarchy
                img_loc (str): location of the medium sized image
                img_name (str): formated the name of the images
                img_path (str): path to the images
                downloaded (bool): flag indicating whether we downloaded the image

        Arguments:
            files (list): list of csv files with database exports from
                the NMS collection
        """
        dfs = [pd.read_csv(f) for f in files]
        col_names = []
        for df in dfs:
            col_names.append(set(df.columns))
        col_names = set.intersection(*col_names)
        
        self.df = pd.concat([df[col_names] for df in dfs], axis=0)
        self.df.drop_duplicates(subset=['priref'], inplace=True) # Check what is the best identifier
        self.df.reset_index(inplace=True)

        col_mapping ={'priref':'record_id', # Check what is the best identifier
         'object_name':'name',
         'object_category':'taxonomy',
         'reproduction.reference':'img_loc'}
        
        self.df.rename(col_mapping, axis=1, inplace=True)
        self.df['img_loc'] = self.df['img_loc'].apply(lambda x: x.split('|') if not pd.isnull(x) else [])
        self.df = self.df.explode('img_loc')
        self.df['img_name'] = self.df.img_loc.apply(lambda x: x + '.jpg' if not pd.isnull(x) else x)
        self.df['img_path'] = self.df.img_name.apply(lambda x: self.img_folder/ x if not pd.isnull(x) else x)
        self.df['downloaded'] = self.df.img_path.apply(lambda x: x.is_file() if not pd.isnull(x) else False)
        self.df['img_path'] = self.df['img_path'].apply(lambda x: str(x))
        self.df = self.df[['record_id','name','description','taxonomy','img_loc','img_name','img_path','downloaded']]

    
    def load_from_csv(self,path_to_csv):
        self.df = pd.read_csv(path_to_csv, index_col=0)

    def fetch_images(self):
        imgs_ids = list(self.df[~self.df['img_loc'].isnull()]['img_loc'])
        imgs_ids = [i for e in imgs_ids for i in e.split('|') if i.startswith('PF')]
        base_url = 'https://www.nms.ac.uk/search.axd?command=getcontent&server=Detail&value='
        for img in tqdm(imgs_ids):
            if (self.img_folder / ( img+'.jpg')).is_file(): 
                continue  
            
            url = base_url + img
            request  = requests.get(url)
            if request.status_code == 200: # check if request is successful  
               
                with open(self.img_folder / ( img+'.jpg'), 'wb') as f:
                    f.write(request.content)
                    time.sleep(random.uniform(.25, .25)) # randomize the requests
                    