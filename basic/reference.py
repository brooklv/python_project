#! /bin/python

print 'Simple Assignment'
shoplist=['apple', 'mange', 'carrot', 'banana']
mylist=shoplist #just point to the same obj
del shoplist[0]

print 'shoplist is',shoplist
print 'mylist is', mylist

print 'copy by making a full slice'
mylist=shoplist[:]   #mylist is a new obj with its own member
del mylist[0]

print 'shoplist is',shoplist
print 'mylist is', mylist

