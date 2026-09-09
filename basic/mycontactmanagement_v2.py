#! /bin/python

import sys
import cPickle as p
#import pickle as p

contactfile = 'contact.data'

class basicInfo:
	'''Request member info'''
	def __init__(self, name, age, sex, category):
		self.name = name
		self.age = age
		self.sex = sex
		self.category = category
	def tell(self):
		'''Tell the details.'''
		print 'Name	:%s'%self.name
		print 'Age	:%d'%self.age
		print 'Sex	:%s'%self.sex
		print 'Category:%s'%self.category
	

class Friend(basicInfo):
	'''Request magic info'''
	def __init__(self, name, age, sex, category, hobby):
		basicInfo.__init__(self, name, age, sex, category)
		self.hobby = hobby
	
	def tell(self):
		basicInfo.tell(self)
		print 'Hobby	:%s'%self.hobby

class Family(basicInfo):
	'''Request nick Name info'''
	def __init__(self, name, age, sex, category, nickName):
		basicInfo.__init__(self, name, age, sex, category)
		self.nickName = nickName

	def tell(self):
		basicInfo.tell(self)
		print 'nickName	:%s'%self.nickName


class Colleague(basicInfo):
	'''Request department info'''
	def __init__(self, name, age, sex, category, department):
		basicInfo.__init__(self, name, age, sex, category)
		self.department = department

	def tell(self):
		basicInfo.tell(self)
		print 'Department	:%s'%department

def searchContact():
	'''Search one person info'''
	contact_dic = {} 				# this is necessary, or it maybe case backPickleGet or EOFErr
	fd = open(contactfile, 'rb')
	try:
		contact_dic = p.load(fd)
	except EOFError:
		#print contact_dic
		pass
	finally:
		fd.close()

	searchName = raw_input('Enter search name-->')
	if len(searchName) == 0:
		print 'Your input is null, system will exit!'
		sys.exit()
	else:
		if contact_dic.has_key(searchName):		# find the person
			print '------------ INFO -----------'
			print 'Name: ',searchName
			info_dic = contact_dic[searchName]
			print 'Age:	',info_dic['age']
			print 'Sex:	',info_dic['sex']
			print 'category: ',info_dic['category']
			if info_dic['category'] == 'friend':
				print 'Hobby: ',info_dic['hobby']
			elif info_dic['category'] == 'family':
				print 'Family: ',info_dic['nickName']
			elif info_dic['category'] == 'colleague':
				print 'Department: ',info_dic['department']
			else:
				pass
			print '-----------------------------'

		else:			# not find the person
			print 'the person %s is not finded in your contact.' %searchName

def addContact():
	'''add new member to contact'''
	contact_dic = {} 				# this is necessary, or it maybe case backPickleGet or EOFErr
	fd = open(contactfile, 'rb')
	try:
		contact_dic = p.load(fd)
	except EOFError:
		#print contact_dic
		pass
	finally:
		fd.close()


	f = file(contactfile, 'w')
	freshName = raw_input('Enter name-->')
	freshAge = raw_input('Enter age-->')
	freshSex = raw_input('Enter sex(you can set man or women)-->')
	freshCategory = raw_input('Enter Category(you can set friend,family,colleague)-->')
	
	if freshCategory == 'friend':
		freshHobby = raw_input('Enter hobby-->')
		
		contact_dic[freshName] = {'name':freshName,'age':freshAge, 'sex':freshSex, 'category':freshCategory, 'hobby':freshHobby}
		
		

	elif freshCategory == 'family':
		freshNickname = raw_input('Enter nickName-->')
		
		contact_dic[freshName] = {'name':freshName, 'age':freshAge, 'sex':freshSex, 'category':freshCategory, 'nickName':freshNickname}
		

	else:
		freshDepartment = raw_input('Enter department-->')
		#contact_dic[freshName] = Colleague(freshName, freshAge, freshSex, freshCategory, freshDepartment)
		contact_dic[freshName] = {'name':freshName, 'age':freshAge, 'sex':freshSex, 'category':freshCategory, 'department':freshDepartment}
		#contact_dic.append({freshName:})

		#p.dump({freshName:Colleague(freshName, freshAge, freshSex, freshCategory, freshDepartment)}, f)
	p.dump(contact_dic, f)
	f.close()
	print 'The person added successfully.'

def modifyContact():
	'''Modify a contact'''
	contact_dic = {} 				# this is necessary, or it maybe case backPickleGet or EOFErr
	fd = open(contactfile, 'rb')
	contact_dic = p.load(fd)
	fd.close()

	fd = open(contactfile, 'r+')
	modifyName = raw_input('Please Enter the modify name-->')
	if len(modifyName) == 0:
		print 'Your input too short, sys will exit'
		fd.close()
		sys.exit()
	else:
		if contact_dic.has_key(modifyName):
			info_dic = {}
			info_dic = contact_dic.get(modifyName)
			
			if info_dic.get('category') == 'friend':
				if raw_input('Change Age?(yes, no):') == 'yes':
					mod_age = raw_input('new age-->')
					info_dic['age'] = mod_age
				if raw_input('Change Sex?(yes, no)') == 'yes':
					mod_sex = raw_input('new sex-->')
					if (mod_sex is not 'man') or (mod_sex is not 'women'):
						print 'your input is valid, keep the origin.'

				if raw_input('Change hobby?(yes, no)') == 'yes':
					mod_hobby = raw_input('new hobby-->')
					info_dic['hobby'] = mod_hobby

			elif info_dic.get('category') == 'family':
				if raw_input('Change Age?(yes, no):') == 'yes':
					mod_age = raw_input('new age-->')
					info_dic['age'] = mod_age
				if raw_input('Change Sex?(yes, no)') == 'yes':
					mod_sex = raw_input('new sex-->')
					if (mod_sex is not 'man') or (mod_sex is not 'women'):
						print 'your input is valid, keep the origin.'

				if raw_input('Change nickName?(yes, no)') == 'yes':
					mod_nickName = raw_input('new nickName-->')
					info_dic['nickName'] = mod_nickName

			elif info_dic.get('category') == 'Colleague':
				if raw_input('Change Age?(yes, no):') == 'yes':
					mod_age = raw_input('new age-->')
					info_dic['age'] = mod_age
				if raw_input('Change Sex?(yes, no)') == 'yes':
					mod_sex = raw_input('new sex-->')
					if mod_sex not in ('man', 'women'):
						print 'your input is valid, keep the origin.'

				if raw_input('Change department?(yes, no)') == 'yes':
					mod_department = raw_input('new department-->')
					info_dic['department'] = mod_department
			else:
				print 'the person is not specify to any category,Please input the category first.'
				mod_category = raw_input('new category(friend, family, Colleague)-->')
				if mod_category not in ('friend', 'family', 'colleague'):
					print 'your input is illegal, sys will out'
					pass
			contact_dic[modifyName] = info_dic
			p.dump(contact_dic, fd)

		else:
			print 'The person is not in your contact, sys will exit.'
			fd.close()
			sys.exit()
		
		fd.close()
		print 'The person modified successfully.'

def delContact():
	''' Delete person from contact.'''
	contact_dic = {} 				# this is necessary, or it maybe case backPickleGet or EOFErr
	fd = open(contactfile, 'rb')
	try:
		contact_dic = p.load(fd)
	except EOFError:
		#print contact_dic
		pass
	finally:
		fd.close()

	fd = open(contactfile, 'w+')
	del_name = raw_input('Please input the delete person-->')
	if len(del_name) == 0:
		print 'Your input is illegal, system will exit.'
	else:
		if contact_dic.has_key(del_name): # the person is in contact
			contact_dic.pop(del_name)
			print 'The person %s is deleted.' %del_name
		else:						# the person is not in your contact
			print 'The person is not in your contact.'
		p.dump(contact_dic, fd)

	fd.close()


def showUsage():
	
	print "This is a contact management \n\
\n\
	your can specify the param as follow:\n\
	--help		:show the usage\n\
	--version	:show the version info\n\
	--search	:search function\n\
	--delete	:delete some one\n\
	--add		:add some one\n\
	--modify	:modfy some info\n\
	"

if len(sys.argv) < 2:
	print 'You should know how to use this.'
	showUsage()
	sys.exit()

if sys.argv[1].startswith('--'):
	option = sys.argv[1][2:]

	if option == 'help':
		showUsage()
	elif option == 'version':
		print 'Version 2.0'
	elif option == 'search':
		searchContact()
	elif option == 'delete':
		delContact()
	elif option == 'modify':
		modifyContact()
	elif option == 'add':
		addContact()
	else:
		print 'Invalid option!!!'
		showUsage()
else:
	print 'Unknown option.'
	sys.exit()



