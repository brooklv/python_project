#! /bin/python

import cPickle as p

shoplistfile='shoplist.data'
shoplist = ['apple', 'mango', 'carrot']

f = file(shoplistfile, 'w')
p.dump(shoplist, f) #dump the object to a file
f.close()

del shoplist
f = file(shoplistfile)
storedlist = p.load(f)
print storedlist

