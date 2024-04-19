# loop operation
import os

# GLOBAL VAR
filelist = []

def get_file(path):
	# LOCAL VAR 
	dirlist = []
	# DECLARE GLOBAL VAR
	global filelist 
	
	# GET ALL FILES IN CURRENT DIRECTORY
	files = os.listdir(path)
	
	# GET THE FILE AND DIRECTORY
	for f in files:
		if(os.path.isdir(path + "/" + f)):
			# OMIT THE HIDDEN DIRECTORY
			if(f[0] == "."):
				pass
			# STORE THE FINDING DIRECTORY
			else:
				dirlist.append(f)
		
		# STORE THE FINDING FILE
		if(os.path.isfile(path + "/" + f)):
			filelist.append(f)

	# RECURSION CALL
	for dl in dirlist:
		get_file(path + "/" + dl)

if __name__ == '__main__':
	get_file(".")
	print "FILE_LIST:\n"
	print filelist
