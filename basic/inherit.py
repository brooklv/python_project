#! /bin/python

class SchoolMember:
	'''Requsets any school member'''
	def __init__(self, name, age):
		self.name = name
		self.age = age
		print '(Initialized School Member:%s)'%self.name
	
	def tell(self):
		'''Tell my details'''
		print 'Name:"%s" Age:"%s"'%(self.name, self.age)

class Teacher(SchoolMember):
	'''Requests a teacher.'''
	def __init__(self, name, age, salary):
		SchoolMember.__init__(self, name, age)
		self.salary = salary
		print '(Initialized Teacher:%s)'%self.name
	
	def tell(self):
		SchoolMember.tell(self)
		print 'Salary:"%d"'%self.salary

class Student(SchoolMember):
	'''Requsets a student.'''
	def __init__(self, name, age, mark):
		SchoolMember.__init__(self, name, age)
		self.mark = mark
		print '(Initialized Student:%s)'%self.name

	def tell(self):
		SchoolMember.tell(self)
		print 'Marks:"%d"'%self.mark

t = Teacher('Mrs.Shrividya', 40, 30000)
s = Student('Swaroop', 22, 75)

print # print a blank line
members = [s, t]
for member in members:
	member.tell()



