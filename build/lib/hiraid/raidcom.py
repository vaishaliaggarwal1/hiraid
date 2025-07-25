#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2021 Hitachi Vantara, Inc. All rights reserved.
# Author: Darren Chambers <@Darren-Chambers>
# Author: Giacomo Chiapparini <@gchiapparini-hv>
# Author: Clive Meakin
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

import re, ast, time, json, logging, subprocess, collections, concurrent.futures, os
from .raidcomparser import Raidcomparser
from .cmdview import Cmdview,CmdviewConcurrent
from .raidcomstats import Raidcomstats
from .storagecapabilities import Storagecapabilities
from .hiraidexception import RaidcomException

from .horcctl import Horcctl
from .inqraid import Inqraid
from . import timer
from .historutils.historutils import Ldevid
from hicciexceptions.cci_exceptions import *
from hicciexceptions.cci_exceptions import cci_exceptions_table
from . import __version__
from typing import Union, Dict, List

class Raidcom:
	version = __version__
	inqraidView = {}
	def __init__(self,serial,instance,path="/usr/bin/",cciextension='.sh',log=logging,username=None,password=None,asyncmode=False,unlockOnException=True,cachedir=f"{os.path.expanduser('~')}{os.sep}hiraid"):

		self.serial = serial
		self.log = log
		self.instances = {}
		self.path = path
		self.cciextension = cciextension
		self.username = username
		self.password = password
		self.cmdoutput = False
		self.views = {}
		self.data = {}
		self.stats = {}
		self.successfulcmds = []
		self.undocmds = []
		self.undodefs = []
		self.parser = Raidcomparser(self,log=self.log)
		self.updatestats = Raidcomstats(self,log=self.log)
		self.asyncmode = asyncmode
		self.lock = None
		self.cachedir = cachedir
		self.cachefile = f"{self.cachedir}{os.sep}{self.serial}_cache.json"
		self.inqraid()
		self.loadinstances(instance)
		self.login()
		self.identify()
		self.limitations()
	
	def loadinstances(self,instances,max_workers=10):
		if isinstance(instances, tuple) or isinstance(instances, list):
			self.instance = instances[0]
			self.instances = { i:{} for i in instances }
		else:
			self.instance = instances
			self.instances[instances] = {}            

		# Obtain unitids concurrently by fetching ports using all instances
		cmdreturn = CmdviewConcurrent()
		with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
			future_out = { executor.submit(self.getport,update_view=False,instance=instance): instance for instance in self.instances}
			for future in concurrent.futures.as_completed(future_out):
				self.instances[future.result().instance]['UNITID'] = future.result().data[0]['UNITID']
				
		with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
			future_out = { executor.submit(self.horcctl,instance=instance,unitid=self.instances[instance]['UNITID']): instance for instance in self.instances}
			for future in concurrent.futures.as_completed(future_out):
				self.update_concurrent_cmdreturn(cmdreturn,future)
				
		for i in self.instances:
			cmd_device = self.views['_horcctl'][i]['current_control_device']
			cmd_device_type = ('FIBRE','IP')['IPCMD' in cmd_device]
			self.instances[i] = { 'cmd_device': cmd_device, 'cmd_device_type': cmd_device_type }
			# if inqraid was successful, we can also return cmd_device_ldevid
			try:
				if cmd_device_type == "FIBRE":
					device_file =  cmd_device.split('/')[-1]
					self.instances[i]['cmd_device_ldevid'] = self.views['_inqraid'][device_file]['LDEV']
					self.instances[i]['cmd_device_culdev'] = Ldevid(self.instances[i]['cmd_device_ldevid']).culdev
					self.instances[i]['cmd_device_port'] = self.views['_inqraid'][device_file]['PORT']
				else:
					self.log.warn(f"Instance {i} is an IPCMD, expect poor performance from this instance")
			except:
				self.log.warn(f"Unable to derive cmd_device ldev_id for instance {i}")
			self.horcm_instance_list = list(self.instances.keys())
			self.num_horcm_instances = len(self.horcm_instance_list)

	def updatetimer(self,cmdreturn):
		elapsedtime = timer.timediff(cmdreturn.start)
		cmdreturn.elapsedseconds = elapsedtime['elapsedseconds']
		cmdreturn.elapsedmilliseconds = elapsedtime['elapsedmilliseconds']
		cmdreturn.end = elapsedtime['end']
		cmdreturn.elapsed = { 'elapsedseconds': elapsedtime['elapsedseconds'], 'elapsedmilliseconds': elapsedtime['elapsedmilliseconds'] }

	def createdir(self,directory):
		if not os.path.exists(directory):
			os.makedirs(directory)

	def writecache(self):
		self.createdir(self.cachedir)
		file = open(self.cachefile,"w")
		file.write(json.dumps(self.views,indent=4))
		return Cmdview(cmd="writecache")

	def loadcache(self):
		self.log.debug(f'Reading cachefile {self.cachefile}')
		try:
			with open(self.cachefile) as json_file:
				self.views = json.load(json_file)
		except Exception as e:
			raise Exception(f'Unable to load cachefile {self.cachefile}')
		return Cmdview(cmd="loadcache")
	
	def updateview(self,view: dict,viewupdate: dict) -> dict:
		''' Update dict view with new dict data '''
		for k, v in viewupdate.items():
			if isinstance(v,collections.abc.Mapping):
				view[k] = self.updateview(view.get(k,{}),v)
			else:
				view[k] = v
		return view

	def login(self,**kwargs):
		if self.username and self.password:
			cmd = f"{self.path}raidcom -login {self.username} {self.password} -I{self.instance}"
			return self.execute(cmd,**kwargs)

	def logout(self,**kwargs):
		cmd = f"{self.path}raidcom -logout -I{self.instance} -s {self.serial}"
		return self.execute(cmd,**kwargs)
	
	def checkport(self,port):
		if not re.search(r'^cl\w-\D+\d?$',port,re.IGNORECASE): raise Exception('Malformed port: {}'.format(port))
		return port
		
	def checkportgid(self,portgid):
		if not re.search(r'cl\w-\D+\d?-\d+',portgid,re.IGNORECASE): raise Exception('Malformed portgid: {}'.format(portgid))
		return portgid

	def getcommandstatus(self,request_id: str=None, **kwargs) -> object:
		'''
		raidcom get command_status\n
		request_id = <optional request_id>
		'''
		requestid_cmd = ('',f"-request_id {request_id}")[request_id is not None]
		cmd = f"{self.path}raidcom get command_status {requestid_cmd} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getcommandstatus(cmdreturn)
		return cmdreturn

	def resetcommandstatus(self, request_id: str='', requestid_cmd='', **kwargs) -> object:
		'''
		raidcom reset command_status
		request_id = <optional request_id>
		'''
		if request_id:
			requestid_cmd = f"-request_id {request_id}"
		cmd = f"{self.path}raidcom reset command_status {requestid_cmd} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		return cmdreturn

	def lockresource(self, **kwargs) -> object:
		'''
		raidcom lock resource -time <seconds>\n
		arguments\n
		time = <seconds>\n
		'''
		time = ('',f"-time {kwargs.get('time')}")[kwargs.get('time') is not None]
		cmd = f"{self.path}raidcom lock resource {time} -I{self.instance} -s {self.serial}"
		undocmd = ['{}raidcom unlock resource -I{} -s {}'.format(self.path,self.instance,self.serial)]
		cmdreturn = self.execute(cmd,undocmd,**kwargs)
		if not cmdreturn.returncode:
			self.lock = True
		return cmdreturn

	def unlockresource(self, **kwargs) -> object:
		cmd = f"{self.path}raidcom unlock resource -I{self.instance} -s {self.serial}"
		undocmd = [f"{self.path}raidcom lock resource -I{self.instance} -s {self.serial}"]
		cmdreturn = self.execute(cmd,undocmd,**kwargs)
		if not cmdreturn.returncode:
			self.lock = False
		return cmdreturn
	
	def horcctl(self, unitid: int, view_keyname: str='_horcctl', **kwargs) -> object:
		cmdreturn = Horcctl(kwargs.get('instance',self.instance)).showControlDeviceOfHorcm(unitid)
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		self.log.debug(f"Storage horcctl (unitid:cmddevice): {cmdreturn.view}")
		return cmdreturn

	def inqraid(self, refresh=False, view_keyname: str='_inqraid', **kwargs) -> object:
		try:
			if getattr(self.inqraidView,'view',None) and not refresh:
				cmdreturn = self.inqraidView
			else:
				cmdreturn = Inqraid().inqraidCli()
				self.__class__.inqraidView = cmdreturn
			self.updateview(self.views,{view_keyname:cmdreturn.view})
			return cmdreturn
		except Exception as e:
			self.log.warn("Unable to obtain inqraid")


	def identify(self, view_keyname: str='_identity', **kwargs) -> object:
		self.concurrent_getresource()
		self.raidqry()
		#self.unitid = self.getport().data[0]['UNITID']
		#self.horcctl(unitid=self.unitid)
		#self.inqraid()
		cmdreturn = self.parser.identify()
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		#Horcctl
		self.log.debug(f"Storage identity: {cmdreturn.view}")
		return cmdreturn

	def raidqry(self, view_keyname: str='_raidqry', **kwargs) -> object:
		'''
		raidqry\n
		examples:\n
		rq = raidqry()\n
		rq = raidqry(datafilter={'Serial#':'350147'})\n
		rq = raidqry(datafilter={'callable':lambda a : int(a['Cache(MB)']) > 50000})\n\n
		Returns Cmdview():\n
		rq.data\n
		rq.view\n
		rq.cmd\n
		rq.returncode\n
		rq.stderr\n
		rq.stdout\n
		rq.stats\n
		'''
		cmd = f"{self.path}raidqry -l -I{self.instance}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.raidqry(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn

	def limitations(self):
		for limitation in Storagecapabilities.default_limitations:
			setattr(self,limitation,Storagecapabilities.limitations.get(self.v_id,{}).get(limitation,Storagecapabilities.default_limitations[limitation]))
   
	def getresource(self, view_keyname: str='_resource_groups', key='opt', **kwargs) -> object:
		optcmd = (f'-key {key}','')[not key or key == '']
		cmd = f"{self.path}raidcom get resource {optcmd} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getresource(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn
	
	def Xgetresource(self, view_keyname: str='_resource_groups', **kwargs) -> object:
		cmd = f"{self.path}raidcom get resource -key opt -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getresource(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn
	
	def concurrent_getresource(self, max_workers: int=5, view_keyname: str='_resource_groups', **kwargs) -> object:

		resource_outputs = []
		def getresource(key=None):
			optcmd = (f'-key {key}','')[not key or key == '']
			cmd = f"{self.path}raidcom get resource {optcmd} -I{self.instance} -s {self.serial}"
			return self.execute(cmd,**kwargs)

		cmdreturn = CmdviewConcurrent()
		with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
			future_out = { executor.submit(getresource,key=key): key for key in ['opt',None]}
			for future in concurrent.futures.as_completed(future_out):
				resource_outputs.append(future.result())
		
		cmdreturn = self.parser.concurrent_getresource(resource_outputs)
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn

	def getresourcebyname(self,view_keyname: str='_resource_groups_named', **kwargs) -> object:
		cmd = f"{self.path}raidcom get resource -key opt -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getresourcebyname(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn

	def getldev(self,ldev_id: str, view_keyname: str='_ldevs', update_view=True, **kwargs) -> object:
		'''
		getldev(ldev_id=1000)
		getldev(ldev_id=1000-11000)
		getldev(ldev_id=1000-11000,datafilter={'LDEV_NAMING':'Test_Label_1'})
		getldev(ldev_id=1000-11000,datafilter={'Anykey_when_val_is_callable':lambda a : float(a.get('Used_Block(GB)',0)) > 10})
		'''
		cmd = f"{self.path}raidcom get ldev -ldev_id {ldev_id} -I{kwargs.get('instance',self.instance)} -s {self.serial}"
		cmdreturn = self.execute(cmd)
		self.parser.getldev(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		if update_view:
			self.updateview(self.views,{view_keyname:cmdreturn.view})
			self.updateview(self.data,{view_keyname:cmdreturn.data})
			self.updatestats.ldevcounts()
		return cmdreturn

	def getldevlist(self, ldevtype: str, view_keyname: str='_ldevlist', update_view=True, key='', **kwargs) -> object:
		'''
		ldevtype = dp_volume | external_volume | journal | pool | parity_grp | mp_blade | defined | undefined | mapped | mapped_nvme | unmapped
		* Some of these options require additional parameters, for example 'pool' requires pool_id = $poolid
		options = { 'key':'front_end' }
		ldevs = getldevlist(ldevtype="mapped",datafilter={'Anykey_when_val_is_callable':lambda a : float(a['Used_Block(GB)']) > 10})\n
		'''
		#options = " ".join([f"-{k} {v}" for k,v in kwargs.get('options',{}).items()])
		key_opt,attr = '',''

		if len(key):
			key_opt = f"-key {key}"
			attr = f"_{key}"

		cmd = f"{self.path}raidcom get ldev -ldev_list {ldevtype} {key_opt} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		#self.parser.getldevlist(cmdreturn,datafilter=kwargs.get('datafilter',{}))

		getattr(self.parser,f'getldevlist{attr}')(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		#self.parser.getattr() getldevlist(cmdreturn,datafilter=kwargs.get('datafilter',{}))

		if update_view:
			self.updateview(self.views,{view_keyname:{ldevtype:cmdreturn.view}})
			self.updatestats.ldevcounts()
			
		self.updatetimer(cmdreturn)    
		return cmdreturn

	def getport(self,view_keyname: str='_ports', update_view=True, **kwargs) -> object:
		'''
		raidcom get port\n
		examples:\n
		ports = getport()\n
		ports = getport(datafilter={'PORT':'CL1-A'})\n
		ports = getport(datafilter={'TYPE':'FIBRE'})\n
		ports = getport(datafilter={'Anykey_when_val_is_callable':lambda a : a['TYPE'] == 'FIBRE' and 'TAR' in a['ATTR']})\n\n
		Returns Cmdview():\n
		ports.data\n
		ports.view\n
		ports.cmd\n
		ports.returncode\n
		ports.stderr\n
		ports.stdout\n
		ports.stats\n
		'''
		cmd = f"{self.path}raidcom get port -I{kwargs.get('instance',self.instance)} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		#self.parser.getport(cmdreturn,datafilter=kwargs.get('datafilter',{}),**kwargs)
		self.parser.getport(cmdreturn,**kwargs)
		if update_view:
			self.updateview(self.views,{view_keyname:cmdreturn.view})
			self.updateview(self.data,{view_keyname:cmdreturn.data})
			self.updatestats.portcounters()

		#self.portcounters()
		#raidcomstats.portcounters(self)
		#self.raidcomstats.portcounters()
		cmdreturn.instance = kwargs.get('instance',self.instance)
		return cmdreturn

	def XXXgethostgrp(self,port: str, view_keyname: str='_ports', update_view: bool=True, **kwargs) -> object:
		'''
		raidcom get host_grp\n
		Better to use gethostgrp_key_detail rather than this function.\n
		You will instead obtain unused host groups and more importantly the resource group id.\n
		raidcom host_grp\n
		examples:\n
		host_grps = gethostgrp(port="cl1-a")\n
		host_grps = gethostgrp(port="cl1-a",datafilter={'HMD':'VMWARE_EX'})\n
		host_grps = gethostgrp(port="cl1-a",datafilter={'GROUP_NAME':'MyGostGroup})\n
		host_grps = gethostgrp(port="cl1-a",datafilter={'Anykey_when_val_is_callable':lambda a : 'TEST' in a['GROUP_NAME'] })\n
		\n
		Returns Cmdview():\n
		host_grps.data\n
		host_grps.view\n
		host_grps.cmd\n
		host_grps.returncode\n
		host_grps.stderr\n
		host_grps.stdout\n
		host_grps.stats\n
		'''
		
		cmd = f"{self.path}raidcom get host_grp -port {port} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.gethostgrp(cmdreturn)
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn

	def gethostgrp(self,port: str, view_keyname: str='_ports', update_view: bool=True, **kwargs) -> object:
		'''
		raidcom host_grp -key detail\n
		examples:\n
		host_grps = gethostgrp_key_detail(port="cl1-a")\n
		host_grps = gethostgrp_key_detail(port="cl1-a-140")\n
		host_grps = gethostgrp_key_detail(port="cl1-a",host_grp_name="MyHostGroup")\n
		host_grps = gethostgrp_key_detail(port="cl1-a",datafilter={'HMD':'VMWARE_EX'})\n
		host_grps = gethostgrp_key_detail(port="cl1-a",datafilter={'GROUP_NAME':'MyGostGroup})\n
		host_grps = gethostgrp_key_detail(port="cl1-a",datafilter={'Anykey_when_val_is_callable':lambda a : 'TEST' in a['GROUP_NAME'] })\n
		\n
		Returns Cmdview():\n
		host_grps.data\n
		host_grps.view\n
		host_grps.cmd\n
		host_grps.returncode\n
		host_grps.stderr\n
		host_grps.stdout\n
		host_grps.stats\n
		'''
		
		'''
		raidcom get host_grp -key detail\n
		Differs slightly from raidcom\n
		If port format cl-port-gid or host_grp_name is supplied with cl-port host_grp is filtered.
		'''
		#cmdreturn = self.gethost_grp_keydetail(port=port,view_keyname=view_keyname,update_view=update_view,**kwargs)
		return self.gethostgrp_key_detail(port=port,view_keyname=view_keyname,update_view=update_view,hostgrp_usage=['_GIDS'],**kwargs)
		

	def gethostgrp_key_detail(self,port: str, view_keyname: str='_ports', update_view: bool=True, **kwargs) -> object:
		'''
		raidcom host_grp -key detail\n
		examples:\n
		host_grps = gethostgrp_key_detail(port="cl1-a")\n
		host_grps = gethostgrp_key_detail(port="cl1-a-140")\n
		host_grps = gethostgrp_key_detail(port="cl1-a",host_grp_name="MyHostGroup")\n
		host_grps = gethostgrp_key_detail(port="cl1-a",datafilter={'HMD':'VMWARE_EX'})\n
		host_grps = gethostgrp_key_detail(port="cl1-a",datafilter={'GROUP_NAME':'MyGostGroup})\n
		host_grps = gethostgrp_key_detail(port="cl1-a",datafilter={'Anykey_when_val_is_callable':lambda a : 'TEST' in a['GROUP_NAME'] })\n
		\n
		Returns Cmdview():\n
		host_grps.data\n
		host_grps.view\n
		host_grps.cmd\n
		host_grps.returncode\n
		host_grps.stderr\n
		host_grps.stdout\n
		host_grps.stats\n
		'''
		
		'''
		raidcom get host_grp -key detail\n
		Differs slightly from raidcom\n
		If port format cl-port-gid or host_grp_name is supplied with cl-port host_grp is filtered.
		'''
		#cmdparam = ""
		
		host_grp_name = kwargs.get('host_grp_name')
		resourceparam = ""
		if re.search(r'cl\w-\D+\d?-\d+',port,re.IGNORECASE):
			if host_grp_name: raise Exception(f"Fully qualified port {port} does not require host_grp_name parameter: {host_grp_name}")
			kwargs['datafilter'] = { 'HOST_GRP_ID': port.upper() } 
		elif host_grp_name:
			#cmdparam = f" -host_grp_name '{host_grp_name}' "
			kwargs['datafilter'] = { 'GROUP_NAME': host_grp_name }

		resource_param = ("",f" -resource {kwargs.get('resource')} ")[kwargs.get('resource') is not None]
		
		cmd = f"{self.path}raidcom get host_grp -port {port} -key detail {resource_param} -I{kwargs.get('instance',self.instance)} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.gethostgrp_key_detail(cmdreturn,datafilter=kwargs.get('datafilter',{}),hostgrp_usage=kwargs.get('hostgrp_usage',['_GIDS','_GIDS_UNUSED']))

		if update_view:
			self.updateview(self.views,{view_keyname:cmdreturn.view})
			self.updatestats.hostgroupcounters()

		return cmdreturn

	def getlun(self,port: str,view_keyname: str='_ports', update_view=True, **kwargs) -> object:
		'''
		raidcom get lun\n
		examples:\n
		luns = getlun(port="cl1-a-1")\n
		luns = getlun(port="cl1-a",host_grp_name="MyHostGroup")\n
		luns = getlun(port="cl1-a",gid=1)\n
		luns = getlun(port="cl1-a-1",datafilter={'LDEV':'12000'})\n
		luns = getlun(port="cl1-a-1",datafilter={'LDEV':['12001','12002']})\n
		luns = getlun(port="cl1-e-1",datafilter={'Anykey_when_val_is_callable':lambda a : int(a['LUN']) > 1})\n
		luns = getlun(port="cl1-e-1",datafilter={'Anykey_when_val_is_callable':lambda a : int(a['LDEV']) > 12000})\n
		\n
		Returns Cmdview():\n
		luns.data\n
		luns.view\n
		luns.cmd\n
		luns.returncode\n
		luns.stderr\n
		luns.stdout\n
		'''
		cmdparam = self.cmdparam(port=port,**kwargs)
		cmd = f"{self.path}raidcom get lun -port {port}{cmdparam} -I{kwargs.get('instance',self.instance)} -s {self.serial} -key opt"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getlun(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		if update_view:
			self.updateview(self.views,{view_keyname:cmdreturn.view})
			self.updatestats.luncounters()
		return cmdreturn

	def cmdparam(self,**kwargs):
		cmdparam = ""
		if re.search(r'cl\w-\D+\d?-\d+',kwargs['port'],re.IGNORECASE):
			if kwargs.get('gid') or kwargs.get('host_grp_name'): raise Exception(f"Fully qualified port {kwargs['port']} does not require gid or host_grp_name '{kwargs}'")
		else:
			if kwargs.get('gid') is None and kwargs.get('host_grp_name') is None: raise Exception("'gid' or 'host_grp_name' is required when port is not fully qualified (cluster-port-gid)")
			if kwargs.get('gid') and kwargs.get('host_grp_name'): raise Exception(f"'gid' and 'host_grp_name' are mutually exclusive, please supply one or the other > 'gid': {kwargs.get('gid')}, 'host_grp_name': {kwargs.get('host_grp_name')}")
			cmdparam = ("-"+str(kwargs.get('gid'))," "+str(kwargs.get('host_grp_name')))[kwargs.get('gid') is None]
		return cmdparam

	def gethbawwn(self,port,view_keyname: str='_ports', update_view=True, **kwargs) -> object:
		'''
		raidcom get hbawwn\n
		examples:\n
		hbawwns = Raidcom.gethbawwn(port="cl1-a-1")\n
		hbawwns = Raidcom.gethbawwn(port="cl1-a",host_grp_name="MyHostGroup")\n
		hbawwns = Raidcom.gethbawwn(port="cl1-a",gid=1)\n
		\n
		Returns Cmdview():\n
		hbawwns.data\n
		hbawwns.view\n
		hbawwns.cmd\n
		hbawwns.returncode\n
		hbawwns.stderr\n
		hbawwns.stdout\n
		'''

		cmdparam = self.cmdparam(port=port,**kwargs)
		cmd = f"{self.path}raidcom get hba_wwn -port {port}{cmdparam} -I{kwargs.get('instance',self.instance)} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.gethbawwn(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		if update_view:
			self.updateview(self.views,{view_keyname:cmdreturn.view})
			self.updatestats.hbawwncounters()
		return cmdreturn


	def getportlogin(self,port: str, view_keyname: str='_ports', update_view=True, **kwargs) -> object:
		'''
		raidcom get port -port {port}\n
		Creates view: self.views['_ports'][port]['PORT_LOGINS'][logged_in_wwn_list].\n
		View is refreshed each time the function is called.\n
		'''
		cmd = f"{self.path}raidcom get port -port {port} -I{kwargs.get('instance',self.instance)} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getportlogin(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		
		if update_view:
			self.updateview(self.views,{view_keyname:cmdreturn.view})
			self.updatestats.portlogincounters()

		return cmdreturn

	def getpool(self, key: str=None, view_keyname: str='_pools', **kwargs) -> object:
		'''
		pools = getpool()\n
		pools = getpool(datafilter={'POOL_NAME':'MyPool'})\n
		pools = getpool(datafilter={'Anykey_when_val_is_callable':lambda a : a['PT'] == 'HDT' or a['PT'] == 'HDP'})\n
		'''
		
		keyswitch = ("",f"-key {key}")[key is not None]
		cmd = f"{self.path}raidcom get pool -I{self.instance} -s {self.serial} {keyswitch}"
		cmdreturn = self.execute(cmd,**kwargs)
		getattr(self.parser,f"getpool_key_{key}")(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		self.updateview(self.data,{view_keyname:cmdreturn.data})
		self.updatestats.poolcounters()
		return cmdreturn

	def getcopygrp(self, view_keyname: str='_copygrps', **kwargs) -> object:
		cmd = f"{self.path}raidcom get copy_grp -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getcopygrp(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn

	def getdevicegrp(self, device_grp_name, view_keyname: str='_devicegrps', **kwargs) -> object:
		cmd = f"{self.path}raidcom get device_grp -device_grp_name {device_grp_name} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getdevicegrp(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn
	
	def getpath(self,view_keyname: str='_paths', update_view=True, **kwargs) -> object:
		'''
		raidcom get path\n
		examples:\n
		paths = getpath()\n
		paths = getpath(datafilter={'Serial#':'53511'})\n
		paths = getport(datafilter={'Anykey_when_val_is_callable':lambda a : a['CM'] != 'NML'})\n\n
		Returns Cmdview():\n
		paths.data\n
		paths.view\n
		paths.cmd\n
		paths.returncode\n
		paths.stderr\n
		paths.stdout\n
		paths.stats\n
		'''
		cmd = f"{self.path}raidcom get path -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getpath(cmdreturn,datafilter=kwargs.get('datafilter',{}),**kwargs)
		if update_view:
			self.updateview(self.views,{view_keyname:cmdreturn.view})
			self.updatestats.portcounters()
		
		return cmdreturn
	
	def getparitygrp(self,view_keyname: str='_parity_grp', update_view=True, **kwargs) -> object:
		'''
		raidcom get parity_grp\n
		examples:\n
		parity_grps = getparitygrp()\n
		parity_grps = getparitygrp(datafilter={'R_TYPE':'14D+2P'})\n
		parity_grps = getparitygrp(datafilter={'Anykey_when_val_is_callable':lambda a : a['DRIVE_TYPE'] != 'DKS5E-J900SS'})\n\n
		Returns Cmdview():\n
		parity_grps.serial\n
		parity_grps.data\n
		parity_grps.view\n
		parity_grps.cmd\n
		parity_grps.returncode\n
		parity_grps.stderr\n
		parity_grps.stdout\n
		parity_grps.stats\n
		'''
		cmd = f"{self.path}raidcom get parity_grp -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getparitygrp(cmdreturn,datafilter=kwargs.get('datafilter',{}),**kwargs)
		if update_view:
			self.updateview(self.views,{view_keyname:cmdreturn.view})
			self.updatestats.portcounters()
		
		return cmdreturn

	def getlicense(self,view_keyname: str='_license', update_view=True, **kwargs) -> object:
		'''
		raidcom get license\n
		examples:\n
		licenses = getlicense()\n
		licenses = getlicense(datafilter={'Type':'PER'})\n
		licenses = getlicense(datafilter={'STS':'INS'})\n
		licenses = getlicense(datafilter={'Anykey_when_val_is_callable':lambda l : 'Migration' in l['Name']})\n\n
		Returns Cmdview():\n
		parity_grps.serial\n
		parity_grps.data\n
		parity_grps.view\n
		parity_grps.cmd\n
		parity_grps.returncode\n
		parity_grps.stderr\n
		parity_grps.stdout\n
		parity_grps.stats\n
		'''
		cmd = f"{self.path}raidcom get license -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getlicense(cmdreturn,datafilter=kwargs.get('datafilter',{}),**kwargs)
		if update_view:
			self.updateview(self.views,{view_keyname:cmdreturn.view})
			self.updatestats.portcounters()
		
		return cmdreturn
	# Snapshots

	def getsnapshot(self, view_keyname: str='_snapshots', **kwargs) -> object:
		# cmd = f"{self.serial}raidcom get snapshot -I{self.instance} -s {self.serial} -format_time"
		cmd = f"{self.path}raidcom get snapshot -I{self.instance} -s {self.serial} -format_time"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getsnapshot(cmdreturn)
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn


	def getsnapshotgroup(self, snapshotgroup: str, fx: str=None, view_keyname: str='_snapshots', **kwargs) -> object:
		fxarg = ("",f"-fx")[fx is not None]
		cmd = f"{self.path}raidcom get snapshot -snapshotgroup {snapshotgroup} -I{self.instance} -s {self.serial} -format_time {fxarg}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getsnapshotgroup(cmdreturn)
		return cmdreturn

	def addsnapshotgroupcascade(self, pvol: str, svol: str, pool: str, snapshotgroup: str, mirror_id: int, snap_mode: str, **kwargs) -> object:
		cmd = f"{self.path}raidcom add snapshot -ldev_id {pvol} {svol} -pool {pool} -snapshotgroup {snapshotgroup} -mirror_id {mirror_id} -snap_mode {snap_mode} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.getcommandstatus()
		return cmdreturn

	def addsnapshotgroup(self, pvol: str, svol: str, pool: str, snapshotgroup: str, **kwargs) -> object:
		cmd = f"{self.path}raidcom add snapshot -ldev_id {pvol} -pool {pool} -snapshotgroup {snapshotgroup} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.getcommandstatus()
		return cmdreturn

	def createsnapshot(self, snapshotgroup: str, **kwargs) -> object:
		cmd = f"{self.path}raidcom modify snapshot -snapshotgroup {snapshotgroup} -snapshot_data create -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.getcommandstatus()
		return cmdreturn

	def unmapsnapshotsvol(self, svol: str, **kwargs) -> object:
		cmd = f"{self.path}raidcom unmap snapshot -ldev_id {svol} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.getcommandstatus()
		return cmdreturn   

	def resyncsnapshotmu(self, pvol: str, mu: int, **kwargs) -> object:
		cmd = f"{self.path}raidcom modify snapshot -ldev_id {pvol} -mirror_id {mu} -snapshot_data resync -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.getcommandstatus()
		return cmdreturn     

	def snapshotevtwait(self, pvol: str, mu: int, checkstatus: str, waittime: int, **kwargs) -> object:
		cmd = f"{self.path}raidcom get snapshot -ldev_id {pvol} -mirror_id {mu} -check_status {checkstatus} -time {waittime} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.getcommandstatus()
		return cmdreturn 

	def snapshotgroupevtwait(self, snapshotgroup: str, checkstatus: str, waittime: int, **kwargs) -> object:
		cmd = f"{self.path}raidcom get snapshot -snapshotgroup {snapshotgroup} -check_status {checkstatus} -time {waittime} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.getcommandstatus()
		return cmdreturn 


	def deletesnapshotmu(self, pvol: str, mu: int, **kwargs) -> object:
		cmd = f"{self.path}raidcom delete snapshot -ldev_id {pvol} -mirror_id {mu} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.getcommandstatus()
		return cmdreturn 

	'''
	commands
	'''
	def addldev_legacy(self,ldev_id: str,poolid: int,capacity: int, return_ldev: bool=True, **kwargs) -> object:
		'''
		raidcom add ldev -ldev_id <Ldev#> -pool <ID#> -capacity <block_size>\n
		examples:\n
		ldev = Raidcom.addldev(ldev_id=12025,poolid=0,capacity=2097152)\n
		ldev = Raidcom.addldev(ldev_id=12025,poolid=0,capacity="1g")\n
		\n
		Returns Cmdview():\n
		ldev.data\n
		ldev.view\n
		ldev.cmd\n
		ldev.undocmds\n
		ldev.returncode\n
		ldev.stderr\n
		ldev.stdout\n
		'''
		cmdparam, ucmdparam, cmddict, ucmddict = '','',{},{}
		options = { 'capacity_saving': ['compression','deduplication_compression','disable'], 'compression_acceleration':['enable','disable'], 'capacity_saving_mode':['inline','postprocess']}

		for arg in kwargs:
			if arg in options:
				if kwargs[arg] not in options[arg]:
					raise Exception(f"Optional command argument {arg} has incorrect value {kwargs[arg]}, possible options are {options[arg]}")
				cmdparam = f"{cmdparam} -{arg} {kwargs[arg]} "
				cmddict[arg] = kwargs[arg]
			if arg == "capacity_saving" and kwargs[arg] != "disable":
				ucmdparam = f"-operation initialize_capacity_saving"
				ucmddict['operation'] = 'initialize_capacity_saving'

		cmd = f"{self.path}raidcom add ldev -ldev_id {ldev_id} -pool {poolid} -capacity {capacity} {cmdparam} -I{self.instance} -s {self.serial}"
		cmddef = { 'cmddef': 'addldev', 'args':{ 'ldev_id':ldev_id, 'poolid':poolid, 'capacity':capacity }.update(cmddict)}

		undocmd = [f"{self.path}raidcom delete ldev -ldev_id {ldev_id} -pool {poolid} -capacity {capacity} {ucmdparam} -I{self.instance} -s {self.serial}"]
		undodef = [{ 'undodef': 'deleteldev', 'args':{ 'ldev_id':ldev_id }.update(ucmddict)}]

		cmdreturn = self.execute(cmd=cmd,undocmds=undocmd,undodefs=undodef,raidcom_asyncronous=True,**kwargs)

		if not kwargs.get('noexec') and return_ldev:
			getldev = self.getldev(ldev_id=ldev_id)
			cmdreturn.data = getldev.data
			cmdreturn.view = getldev.view
		return cmdreturn
	

	def addldev(self,ldev_id: str,poolid: int,capacity: int, return_ldev: bool=True, start: int=None, end: int=None, **kwargs) -> object:
		'''
		raidcom add ldev -ldev_id <Ldev#> -pool <ID#> -capacity <block_size>\n
		examples:\n
		ldev = Raidcom.addldev(ldev_id=12025,poolid=0,capacity=2097152)\n
		ldev = Raidcom.addldev(ldev_id=12025,poolid=0,capacity="1g")\n
		ldev = Raidcom.addldev(ldev_id='auto',poolid=0,start=1000,end=2000,capacity="1g",capacity_saving="compression")\n
		\n
		Returns Cmdview():\n
		ldev.data\n
		ldev.view\n
		ldev.cmd\n
		ldev.undocmds\n
		ldev.returncode\n
		ldev.stderr\n
		ldev.stdout\n
		# Add -cylinder and -emulation options. Darren Chambers 25/03/2025
		'''
		def validate_cylinder(value):
			if not isinstance(value, int) or value <= 1:
				raise ValueError(f"Cylinder must be a positive integer, got {value}")
			return True
	
		cmdreturn = Cmdview(cmd="addldev")
		cmdparam, ucmdparam, cmddict, ucmddict = '','',{},{}
	
		# Define validators for each option type
		validate = {
			'capacity_saving': lambda x: x in ['compression', 'deduplication_compression', 'disable'],
			'compression_acceleration': lambda x: x in ['enable', 'disable'],
			'capacity_saving_mode': lambda x: x in ['inline', 'postprocess'],
			'drs': lambda x: isinstance(x, bool),
			'cylinder': validate_cylinder,
			'emulation': lambda x: x in ['3390-A']
			}

		# Validate and build command parameters
		for arg, value in kwargs.items():
			if arg in validate:
				try:
					if not validate[arg](value):
						raise ValueError(f"Invalid value for {arg}: {value}")
				except ValueError as e:
					if self.asyncmode:
						cmdreturn.returncode = 999
						cmdreturn.stderr = str(e)
						return cmdreturn
					raise

			if isinstance(value, bool):
				cmdparam = (cmdparam, f"{cmdparam} -{arg} ")[value]
			else:
				cmdparam = f"{cmdparam} -{arg} {value} "
				cmddict[arg] = value
			
			if arg == "capacity_saving" and kwargs[arg] != "disable":
				ucmdparam = f"-operation initialize_capacity_saving"
				ucmddict['operation'] = 'initialize_capacity_saving'

		if ldev_id == 'auto':
			if not start or not end:
				message = f"When ldev_id is specified as 'auto' range_start and range_end ldev_ids must also be supplied"
				if self.asyncmode:
					cmdreturn.returncode = 999
					cmdreturn.stderr = message
					return cmdreturn
				raise Exception(message)
			else:
				cmd = f"{self.path}raidcom add ldev -ldev_id auto -ldev_range {start}-{end} -pool {poolid} -capacity {capacity} {cmdparam} -request_id auto -I{self.instance} -s {self.serial}"
				cmdreturn = self.execute(cmd=cmd,raidcom_asyncronous=False,**kwargs)
		else:
			cmd = f"{self.path}raidcom add ldev -ldev_id auto -ldev_range {ldev_id}-{ldev_id} -pool {poolid} -capacity {capacity} {cmdparam} -request_id auto -I{self.instance} -s {self.serial}"
			cmdreturn = self.execute(cmd=cmd,raidcom_asyncronous=False,**kwargs)
		
		reqid = cmdreturn.stdout.rstrip().split(' : ')
		if not re.search(r'REQID',reqid[0]):
			if self.asyncmode:
				message = f"Unable to obtain REQID from stdout {cmdreturn}"
				cmdreturn.returncode = 999
				cmdreturn.stderr = message
				return cmdreturn
			else:
				raise Exception(message)
		try:
			getcommandstatus = self.getcommandstatus(request_id=reqid[1])
			self.parser.getcommandstatus(getcommandstatus)
			auto_ldev_id = getcommandstatus.data[0]['ID']
			undocmd = f"{self.path}raidcom delete ldev -ldev_id {auto_ldev_id} -pool {poolid} -capacity {capacity} {ucmdparam} -I{self.instance} -s {self.serial}"
			undodef = { 'undodef': 'deleteldev', 'args':{ 'ldev_id':auto_ldev_id }}
			cmdreturn.undocmds.insert(0,undocmd)
			cmdreturn.undodefs.insert(0,undodef)
			echo = f'echo "Executing: {undocmd}"'
			self.undocmds.insert(0,undocmd)
			self.undocmds.insert(0,echo)
			self.resetcommandstatus(request_id=reqid[1])
		except Exception as e:
			if self.asyncmode:
				return cmdreturn
			else:
				raise Exception(f"Failed to create ldev {ldev_id}, request_id {reqid[1]} error {e}")

		if not kwargs.get('noexec') and return_ldev and (cmdreturn.returncode == cmdreturn.expectedreturn):
			getldev = self.getldev(ldev_id=auto_ldev_id)
			cmdreturn.data = getldev.data
			cmdreturn.view = getldev.view
		
		return cmdreturn
	
	def addvvolmf(self, ldev_id: str, poolid: int, cylinder: int, emulation: str='3390-A', return_ldev: bool=True, start: int=None, end: int=None, **kwargs) -> object:
		'''
		raidcom add ldev -ldev_id <Ldev#> -pool <ID#> -cylinder <size> -emulation <emulation type>\n
		For creating mainframe VVOLs using cylinder size and emulation type parameters.\n
		examples:\n
		ldev = Raidcom.addvvolmf(ldev_id=12025, poolid=0, cylinder=1000, emulation='3390-A')\n
		ldev = Raidcom.addvvolmf(ldev_id=12025, poolid=0, cylinder='10m', emulation='3390-A')\n
		ldev = Raidcom.addvvolmf(ldev_id='auto', poolid=0, start=1000, end=2000, cylinder=1000, emulation='3390-A')\n
		\n
		Returns Cmdview():\n
		ldev.data\n
		ldev.view\n
		ldev.cmd\n
		ldev.undocmds\n
		ldev.returncode\n
		ldev.stderr\n
		ldev.stdout\n
		'''
		def validate_cylinder(value):
			if isinstance(value, int) and value <= 1:
				raise ValueError(f"Cylinder must be a positive integer greater than 1, got {value}")
			return True
			
		def validate_emulation(value):
			valid_emulations = ['3390-A', '3390-3', '3390-3R', '3390-9', '3390-L', '3390-M', '3390-V']
			if value not in valid_emulations:
				raise ValueError(f"Invalid emulation type: {value}. Valid types are: {', '.join(valid_emulations)}")
			return True
	
		cmdreturn = Cmdview(cmd="addvvolmf")
		cmdparam, ucmdparam, cmddict, ucmddict = '', '', {}, {}
	
		# Define validators for each option type
		validate = {
			'capacity_saving': lambda x: x in ['compression', 'deduplication_compression', 'disable'],
			'compression_acceleration': lambda x: x in ['enable', 'disable'],
			'capacity_saving_mode': lambda x: x in ['inline', 'postprocess'],
			'drs': lambda x: isinstance(x, bool),
		}

		# Validate emulation type
		try:
			validate_emulation(emulation)
		except ValueError as e:
			if self.asyncmode:
				cmdreturn.returncode = 999
				cmdreturn.stderr = str(e)
				return cmdreturn
			raise

		# Validate and build command parameters
		for arg, value in kwargs.items():
			if arg in validate:
				try:
					if not validate[arg](value):
						raise ValueError(f"Invalid value for {arg}: {value}")
				except ValueError as e:
					if self.asyncmode:
						cmdreturn.returncode = 999
						cmdreturn.stderr = str(e)
						return cmdreturn
					raise

			if isinstance(value, bool):
				cmdparam = (cmdparam, f"{cmdparam} -{arg} ")[value]
			else:
				cmdparam = f"{cmdparam} -{arg} {value} "
				cmddict[arg] = value
			
			if arg == "capacity_saving" and kwargs[arg] != "disable":
				ucmdparam = f"-operation initialize_capacity_saving"
				ucmddict['operation'] = 'initialize_capacity_saving'

		if ldev_id == 'auto':
			if not start or not end:
				message = "When ldev_id is specified as 'auto' range_start and range_end ldev_ids must also be supplied"
				if self.asyncmode:
					cmdreturn.returncode = 999
					cmdreturn.stderr = message
					return cmdreturn
				raise Exception(message)
			else:
				cmd = f"{self.path}raidcom add ldev -ldev_id auto -ldev_range {start}-{end} -pool {poolid} -cylinder {cylinder} -emulation {emulation} {cmdparam} -request_id auto -I{self.instance} -s {self.serial}"
				cmdreturn = self.execute(cmd=cmd, raidcom_asyncronous=False, **kwargs)
		else:
			cmd = f"{self.path}raidcom add ldev -ldev_id {ldev_id} -pool {poolid} -cylinder {cylinder} -emulation {emulation} {cmdparam} -request_id auto -I{self.instance} -s {self.serial}"
			cmdreturn = self.execute(cmd=cmd, raidcom_asyncronous=False, **kwargs)
		
		reqid = cmdreturn.stdout.rstrip().split(' : ')
		if not re.search(r'REQID', reqid[0]):
			message = f"Unable to obtain REQID from stdout {cmdreturn}"
			if self.asyncmode:
				cmdreturn.returncode = 999
				cmdreturn.stderr = message
				return cmdreturn
			else:
				raise Exception(message)
		
		try:
			getcommandstatus = self.getcommandstatus(request_id=reqid[1])
			self.parser.getcommandstatus(getcommandstatus)
			auto_ldev_id = getcommandstatus.data[0]['ID']
			
			# For undo command, we need the actual LDEV ID that was created
			undocmd = f"{self.path}raidcom delete ldev -ldev_id {auto_ldev_id} {ucmdparam} -I{self.instance} -s {self.serial}"
			undodef = {'undodef': 'deleteldev', 'args': {'ldev_id': auto_ldev_id}}
			cmdreturn.undocmds.insert(0, undocmd)
			cmdreturn.undodefs.insert(0, undodef)
			echo = f'echo "Executing: {undocmd}"'
			self.undocmds.insert(0, undocmd)
			self.undocmds.insert(0, echo)
			self.resetcommandstatus(request_id=reqid[1])
		except Exception as e:
			if self.asyncmode:
				return cmdreturn
			else:
				raise Exception(f"Failed to create mainframe VVOL {ldev_id}, request_id {reqid[1]} error {e}")

		if not kwargs.get('noexec') and return_ldev and (cmdreturn.returncode == cmdreturn.expectedreturn):
			getldev = self.getldev(ldev_id=auto_ldev_id)
			cmdreturn.data = getldev.data
			cmdreturn.view = getldev.view
		
		return cmdreturn

	def addmfvvol(self,ldev_id: str,poolid: int,cylinder: int, emulation: str, return_ldev: bool=True, **kwargs) -> object:
		def validate_cylinder(value):
			if not isinstance(value, int) or value <= 1:
				raise ValueError(f"Cylinder must be a positive integer, got {value}")
			return True
	
		cmdreturn = Cmdview(cmd="addmfvvol")
		cmdparam, ucmdparam, cmddict, ucmddict = '','',{},{}
	
		# Define validators for each option type
		validate = {
			'capacity_saving': lambda x: x in ['compression', 'deduplication_compression', 'disable'],
			'compression_acceleration': lambda x: x in ['enable', 'disable'],
			'capacity_saving_mode': lambda x: x in ['inline', 'postprocess'],
			'drs': lambda x: isinstance(x, bool),
			'cylinder': validate_cylinder,
			'emulation': lambda x: x in ['3390-A']
			}

		# Validate and build command parameters
		for arg, value in kwargs.items():
			if arg in validate:
				try:
					if not validate[arg](value):
						raise ValueError(f"Invalid value for {arg}: {value}")
				except ValueError as e:
					if self.asyncmode:
						cmdreturn.returncode = 999
						cmdreturn.stderr = str(e)
						return cmdreturn
					raise

			if isinstance(value, bool):
				cmdparam = (cmdparam, f"{cmdparam} -{arg} ")[value]
			else:
				cmdparam = f"{cmdparam} -{arg} {value} "
				cmddict[arg] = value
			
			if arg == "capacity_saving" and kwargs[arg] != "disable":
				ucmdparam = f"-operation initialize_capacity_saving"
				ucmddict['operation'] = 'initialize_capacity_saving'

		if ldev_id == 'auto':
			if not start or not end:
				message = f"When ldev_id is specified as 'auto' range_start and range_end ldev_ids must also be supplied"
				if self.asyncmode:
					cmdreturn.returncode = 999
					cmdreturn.stderr = message
					return cmdreturn
				raise Exception(message)
			else:
				cmd = f"{self.path}raidcom add ldev -ldev_id auto -ldev_range {start}-{end} -pool {poolid} -cylinder {cylinder} -emulation {emulation} {cmdparam} -request_id auto -I{self.instance} -s {self.serial}"
				cmdreturn = self.execute(cmd=cmd,raidcom_asyncronous=False,**kwargs)
		else:
			cmd = f"{self.path}raidcom add ldev -ldev_id {ldev_id} -pool {poolid} -cylinder {cylinder} -emulation {emulation} {cmdparam} -request_id auto -I{self.instance} -s {self.serial}"
			cmdreturn = self.execute(cmd=cmd,raidcom_asyncronous=False,**kwargs)
		
		reqid = cmdreturn.stdout.rstrip().split(' : ')
		if not re.search(r'REQID',reqid[0]):
			if self.asyncmode:
				message = f"Unable to obtain REQID from stdout {cmdreturn}"
				cmdreturn.returncode = 999
				cmdreturn.stderr = message
				return cmdreturn
			else:
				raise Exception(message)
		try:
			getcommandstatus = self.getcommandstatus(request_id=reqid[1])
			self.parser.getcommandstatus(getcommandstatus)
			auto_ldev_id = getcommandstatus.data[0]['ID']
			undocmd = f"{self.path}raidcom delete ldev -ldev_id {auto_ldev_id} -pool {poolid} -capacity {capacity} {ucmdparam} -I{self.instance} -s {self.serial}"
			undodef = { 'undodef': 'deleteldev', 'args':{ 'ldev_id':auto_ldev_id }}
			cmdreturn.undocmds.insert(0, undocmd)
			cmdreturn.undodefs.insert(0, undodef)
			echo = f'echo "Executing: {undocmd}"'
			self.undocmds.insert(0, undocmd)
			self.undocmds.insert(0, echo)
			self.resetcommandstatus(request_id=reqid[1])
		except Exception as e:
			if self.asyncmode:
				return cmdreturn
			else:
				raise Exception(f"Failed to create ldev {ldev_id}, request_id {reqid[1]} error {e}")

		if not kwargs.get('noexec') and return_ldev and (cmdreturn.returncode == cmdreturn.expectedreturn):
			getldev = self.getldev(ldev_id=auto_ldev_id)
			cmdreturn.data = getldev.data
			cmdreturn.view = getldev.view
		
		return cmdreturn

	# end

	def addldevnew(self,ldev_id: str,poolid: int,capacity: int, return_ldev: bool=True, start: int=None, end: int=None, **kwargs) -> object:
		'''
		raidcom add ldev -ldev_id <Ldev#> -pool <ID#> -capacity <block_size>\n
		examples:\n
		ldev = Raidcom.addldev(ldev_id=12025,poolid=0,capacity=2097152)\n
		ldev = Raidcom.addldev(ldev_id=12025,poolid=0,capacity="1g")\n
		ldev = Raidcom.addldev(ldev_id='auto',poolid=0,start=1000,end=2000,capacity="1g",capacity_saving="compression")\n
		\n
		Returns Cmdview():\n
		ldev.data\n
		ldev.view\n
		ldev.cmd\n
		ldev.undocmds\n
		ldev.returncode\n
		ldev.stderr\n
		ldev.stdout\n
		'''
		cmdreturn = Cmdview(cmd="addldev")
		cmdparam, ucmdparam, cmddict, ucmddict = '','',{},{}
		options = { 'capacity_saving': ['compression','deduplication_compression','disable'], 'compression_acceleration':['enable','disable'], 'capacity_saving_mode':['inline','postprocess']}

		def log(cmdreturn):
			self.log.error(f"Return > {cmdreturn.returncode}")
			self.log.error(f"Stdout > {cmdreturn.stdout}")
			self.log.error(f"Stderr > {cmdreturn.stderr}")

		try:
			for arg in kwargs:
				if arg in options and kwargs[arg] not in options[arg]:
					cmdreturn.stderr,cmdreturn.returncode = f"Optional command argument {arg} has incorrect value {kwargs[arg]}, possible options supported by this function are {options[arg]}",999
					log(cmdreturn)
					raise Exception(cmdreturn.stderr)
				else:
					cmdparam = f"{cmdparam} -{arg} {kwargs[arg]} "
					cmddict[arg] = kwargs[arg]
				if arg == "capacity_saving" and kwargs[arg] != "disable":
					ucmdparam = f"-operation initialize_capacity_saving"
					ucmddict['operation'] = 'initialize_capacity_saving'

			if ldev_id == 'auto':
				if not start or not end:
					cmdreturn.stderr,cmdreturn.returncode = f"When ldev_id is specified as 'auto' range_start and range_end ldev_ids must also be supplied",999
					log(cmdreturn)
					raise Exception(cmdreturn.stderr)
				else:
					cmd = f"{self.path}raidcom add ldev -ldev_id auto -ldev_range {start}-{end} -pool {poolid} -capacity {capacity} {cmdparam} -request_id auto -I{self.instance} -s {self.serial}"
			else:
				cmd = f"{self.path}raidcom add ldev -ldev_id auto -ldev_range {ldev_id}-{ldev_id} -pool {poolid} -capacity {capacity} {cmdparam} -request_id auto -I{self.instance} -s {self.serial}"
		
		#except Exception as e:
		except Exception as e:
			if self.asyncmode:
				return cmdreturn
			else:
				raise RaidcomException(f"Failed to create ldev {ldev_id} - error {e}",self)

		try:
			# execute function uses getcommandstatus without request_id so we need to turn it off and check
			cmdreturn = self.execute(cmd=cmd,raidcom_asyncronous=False,**kwargs)
			reqid = cmdreturn.stdout.rstrip().split(' : ')
			
			if not re.search(r'REQID',reqid[0]):
				cmdreturn.stderr = f"Unable to obtain REQID from stdout {cmdreturn}"
				raise Exception(cmdreturn.stderr)
			getcommandstatus = self.getcommandstatus(request_id=reqid[1])
			if getcommandstatus.returncode:
				cmdreturn.stderr = getcommandstatus.stdout
				cmdreturn.returncode = getcommandstatus.returncode
				cmdreturn.view = getcommandstatus.view
				cmdreturn.data = getcommandstatus.data
				raise Exception(cmdreturn.stderr)
			else:
				created_ldev_id = getcommandstatus.data[0]['ID']
				getldev = self.getldev(ldev_id=created_ldev_id)
				cmdreturn.data = getldev.data
				cmdreturn.view = getldev.view
		except Exception as e:
			if self.asyncmode:
				return cmdreturn
			else:
				raise RaidcomException(f"Failed to create ldev {ldev_id} - error {e}",self)
		#finally:
		return cmdreturn

	def extendldev(self, ldev_id: str, capacity: int, **kwargs) -> object:
		'''
		ldev_id   = Ldevid to extend\n
		capacity = capacity in blk\n
		Where 'capacity' will add specified blks to current capacity 
		'''
		#self.resetcommandstatus()
		cmd = f"{self.path}raidcom extend ldev -ldev_id {ldev_id} -capacity {capacity} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,raidcom_asyncronous=True,**kwargs)
		#self.getcommandstatus()
		return cmdreturn

	def populateundo(self,undodef,undocmds,undodefs):
		undocmds.insert(0,getattr(self,undodef['undodef'])(noexec=True,**undodef['args']).cmd)
		undodefs.insert(0,undodef)

	def deleteldev_undo(self,ldev_id: str, **kwargs):
		ldev = self.getldev(ldev_id=ldev_id)
		undocmds = []
		undodefs = []
		if len(ldev.data[0].get('LDEV_NAMING',"")):
			self.populateundo({'undodef':'modifyldevname','args':{'ldev_id':ldev_id,'ldev_name':ldev.data[0]['LDEV_NAMING']}},undocmds,undodefs)
		if ldev.data[0]['VOL_TYPE'] != "NOT DEFINED":
			self.populateundo({'undodef':'addldev','args':{'ldev_id':ldev_id,'capacity':ldev.data[0]['VOL_Capacity(BLK)'],'poolid':ldev.data[0]['B_POOLID']}},undocmds,undodefs)
		return undocmds,undodefs,ldev
			
	def deleteldev(self,ldev_id: str, **kwargs) -> object:
		cmd = f"{self.path}raidcom delete ldev -ldev_id {ldev_id} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,raidcom_asyncronous=True,**kwargs)
		return cmdreturn

	def addresource(self,resource_name: str,virtualSerialNumber: str=None,virtualModel: str=None, **kwargs) -> object:
		undocmd = [f"{self.path}raidcom delete resource -resource_name '{resource_name}' -I{self.instance} -s {self.serial}"]
		undodef = [{'undodef':'deleteresource','args':{'resource_name':resource_name}}]
		cmd = f"{self.path}raidcom add resource -resource_name '{resource_name}' -virtual_type {virtualSerialNumber} {virtualModel} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,undocmd,undodef,**kwargs)
		return cmdreturn

	def deleteresource_undo(self,resource_name: str,**kwargs):
		undocmds = []
		undodefs = []
		resource_data = self.getresourcebyname()
		rgrp = resource_data.view[resource_name]
		self.populateundo({'undodef':'addresource','args':{'resource_name':rgrp['RS_GROUP'],'virtualSerialNumber':rgrp['V_Serial#'],'virtualModel':rgrp['V_ID']}},undocmds,undodefs)
		return undocmds,undodefs
	
	def deleteresource(self,resource_name: str, **kwargs) -> object:
		undocmds,undodefs = self.deleteresource_undo(resource_name=resource_name,**kwargs)
		cmd = f"{self.path}raidcom delete resource -resource_name '{resource_name}' -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,undocmds,undodefs,**kwargs)
		return cmdreturn

	def addhostgrpresource(self,port: str,resource_name: str, **kwargs) -> object:
		cmdparam = self.cmdparam(port=port,**kwargs)
		cmd = f"{self.path}raidcom add resource -resource_name '{resource_name}' -port {port}{cmdparam} -I{self.instance} -s {self.serial}"
		undocmd = [f"{self.path}raidcom delete resource -resource_name '{resource_name}' -port {port}{cmdparam} -I{self.instance} -s {self.serial}"]
		cmdreturn = self.execute(cmd,undocmd,**kwargs)
		return cmdreturn

	def deletehostgrpresourceid(self,port: str,resource_id: str, **kwargs) -> object:
		cmdparam = self.cmdparam(port=port,**kwargs)
		resource_name = self.views['_resource_groups'][str(resource_id)]['RS_GROUP']
		cmd = f"{self.path}raidcom delete resource -resource_name '{resource_name}' -port {port}{cmdparam} -I{self.instance} -s {self.serial}"
		undocmd = [f"{self.path}raidcom add resource -resource_name '{resource_name}' -port {port}{cmdparam} -I{self.instance} -s {self.serial}"]
		cmdreturn = self.execute(cmd,undocmd,**kwargs)
		return cmdreturn

	def addhostgrpresourceid(self,port: str,resource_id: str, **kwargs) -> object:
		cmdparam = self.cmdparam(port=port,**kwargs)
		resource_name = self.views['_resource_groups'][str(resource_id)]['RS_GROUP']
		cmd = f"{self.path}raidcom add resource -resource_name '{resource_name}' -port {port}{cmdparam} -I{self.instance} -s {self.serial}"
		undocmd = [f"{self.path}raidcom delete resource -resource_name '{resource_name}' -port {port}{cmdparam} -I{self.instance} -s {self.serial}"]
		cmdreturn = self.execute(cmd,undocmd,**kwargs)
		return cmdreturn
	
	def addldevresource(self, resource_name: str, ldev_id: str, **kwargs) -> object:
		cmd = f"{self.path}raidcom add resource -resource_name '{resource_name}' -ldev_id {ldev_id} -I{self.instance} -s {self.serial}"
		undocmd = [f"{self.path}raidcom delete resource -resource_name '{resource_name}' -ldev_id {ldev_id} -I{self.instance} -s {self.serial}"]
		cmdreturn = self.execute(cmd,undocmd,**kwargs)
		return cmdreturn
	
	def deleteldevresourceid(self, resource_id: int, ldev_id: str, **kwargs) -> object:
		#resource_name = self.getresource().view[str(resource_id)]['RS_GROUP']
		resource_name = self.views['_resource_groups'][str(resource_id)]['RS_GROUP']
		cmdreturn = self.deleteldevresource(resource_name=resource_name,ldev_id=ldev_id,**kwargs)
		return cmdreturn

	def deleteldevresource(self, resource_name: str, ldev_id: str, **kwargs) -> object:
		cmd = f"{self.path}raidcom delete resource -resource_name '{resource_name}' -ldev_id {ldev_id} -I{self.instance} -s {self.serial}"
		undocmd = [f"{self.path}raidcom add resource -resource_name '{resource_name}' -ldev_id {ldev_id} -I{self.instance} -s {self.serial}"]
		cmdreturn = self.execute(cmd,undocmd,**kwargs)
		return cmdreturn

	def addhostgrp(self,port: str,host_grp_name: str, **kwargs) -> object:
		cmd = f"{self.path}raidcom add host_grp -host_grp_name '{host_grp_name}' -port {port} -I{self.instance} -s {self.serial}"
		undocmd = [f"{self.path}raidcom delete host_grp -port {'-'.join(port.split('-')[:2])} '{host_grp_name}' -I{self.instance} -s {self.serial}"]
		cmdreturn = self.execute(cmd,undocmd,**kwargs)
		if not kwargs.get('noexec') and (cmdreturn.returncode == cmdreturn.expectedreturn):
			host_grp = self.gethostgrp_key_detail(port='-'.join(port.split('-')[:2]),host_grp_name=host_grp_name)
			cmdreturn.data = host_grp.data
			cmdreturn.view = host_grp.view
		return cmdreturn

	def addhostgroup(self,port: str,hostgroupname: str, **kwargs) -> object:
		'''Deprecated in favour of addhostgrp'''
		return self.addhostgrp(port=port,host_grp_name=hostgroupname,**kwargs)

	def deletehostgrp_undo(self,port:str, **kwargs) -> object:
		undocmds = []
		undodefs = []

		def populateundo(undodef):
			undocmds.insert(0,getattr(self,undodef['undodef'])(noexec=True,**undodef['args']).cmd)
			undodefs.insert(0,undodef)
		
		_host_grp_detail = self.gethostgrp_key_detail(port=port,**kwargs)

		if len(_host_grp_detail.data) < 1 or _host_grp_detail.data[0]['GROUP_NAME'] == "-":
			self.log.warning(f"Host group does not exist > port: '{port}', kwargs: '{kwargs}', data: {_host_grp_detail.data}")
			return undocmds, undodefs
		if len(_host_grp_detail.data) > 1:
			raise Exception(f"Incorrect number of host groups returned {len(_host_grp_detail.data)}, cannot exceed 1. {_host_grp_detail.data}")

		for host_grp in _host_grp_detail.data:
			populateundo({'undodef':'addhostgrp', 'args':{'port':host_grp['HOST_GRP_ID'], 'host_grp_name':host_grp['GROUP_NAME']}})

			if len(host_grp['HMO_BITs']):
				populateundo({'undodef':'modifyhostgrp', 'args':{'port':host_grp['PORT'], 'host_grp_name':host_grp['GROUP_NAME'], 'host_mode': host_grp['HMD'].replace('/IRIX',''), 'host_mode_opt':host_grp['HMO_BITs']}})
				
			if host_grp['RGID'] != "0":
				resource_name = self.views['_resource_groups'][host_grp['RGID']]['RS_GROUP']
				populateundo({'undodef':'addhostgrpresource', 'args':{'port':host_grp['PORT'], 'host_grp_name':host_grp['GROUP_NAME'], 'resource_name':resource_name}})
			
			# luns
			luns = self.getlun(port=port,**kwargs)
			for lun in luns.data:
				populateundo({'undodef':'addlun','args':{'port':lun['PORT'],'host_grp_name':host_grp['GROUP_NAME'],'ldev_id':lun['LDEV'],'lun_id':lun['LUN']}})
			
			# hba_wwns
			hbawwns = self.gethbawwn(port=port,**kwargs)
			for hbawwn in hbawwns.data:
				populateundo({'undodef':'addhbawwn','args':{'port':hbawwn['PORT'],'host_grp_name':host_grp['GROUP_NAME'],'hba_wwn':hbawwn['HWWN']}})
				if hbawwn['NICK_NAME'] != "-":
					populateundo({'undodef':'setwwnnickname','args':{'port':hbawwn['PORT'],'host_grp_name':host_grp['GROUP_NAME'],'hba_wwn':hbawwn['HWWN'],'wwn_nickname':hbawwn['NICK_NAME']}})

		return undocmds,undodefs

	def deletehostgrp(self,port: str, **kwargs) -> object:
		cmdparam = self.cmdparam(port=port,**kwargs)
		'''
		cmdparam = ""
		host_grp_name = kwargs.get('host_grp_name')
		if re.search(r'cl\w-\D+\d?-\d+',port,re.IGNORECASE):
			if host_grp_name: raise Exception(f"Fully qualified port {port} does not require host_grp_name parameter: {host_grp_name}")
		else:
			if not host_grp_name:
				raise Exception("Without a fully qualified port (cluster-port-gid) host_grp_name parameter is required.")
			else:
				cmdparam = f" '{host_grp_name}' "
		'''
		undocmds,undodefs = self.deletehostgrp_undo(port=port,**kwargs)
		cmd = f"{self.path}raidcom delete host_grp -port {port}{cmdparam} -I{self.instance} -s {self.serial}"    
		if len(undocmds):
			cmdreturn = self.execute(cmd,undocmds,undodefs,**kwargs)
		else:
			self.log.warning(f"Host group does not appear to exist - port: '{port}', kwargs: '{kwargs}'. Returning quietly")
			cmdreturn = self.execute(cmd,undocmds,undodefs,noexec=True)

		return cmdreturn

	def resethostgrp(self,port: str, **kwargs) -> object:
		self.deletehostgrp(port=port,**kwargs)

	def addldevauto(self,poolid: int,capacity: int,start: int,end: int, **kwargs):
		ldev_range = '{}-{}'.format(start,end)
		self.resetcommandstatus()
		cmd = f"{self.path}raidcom add ldev -ldev_id auto -request_id auto -ldev_range {ldev_range} -pool {poolid} -capacity {capacity} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		reqid = cmdreturn.stdout.rstrip().split(' : ')
		if not re.search(r'REQID',reqid[0]):
			raise Exception(f"Unable to obtain REQID from stdout {cmdreturn}.")
		getcommandstatus = self.getcommandstatus(request_id=reqid[1])
		self.parser.getcommandstatus(getcommandstatus)
		auto_ldev_id = getcommandstatus.data[0]['ID']
		undodef = {'undodef':'deleteldev','args':{'ldev_id':auto_ldev_id}}

		cmdreturn.view = undo.view
		cmdreturn.data = undo.data
		cmdreturn.undocmds.insert(0,undo.cmd)
		cmdreturn.undodefs.insert(0,undodef)
		self.undocmds.insert(0,cmdreturn.undocmds[0])
		self.undocmds.insert(0,f'echo "Executing: {cmdreturn.undocmds[0]}"')
		self.undodefs.insert(0,cmdreturn.undodefs[0])
		# Reset command status
		self.resetcommandstatus(reqid[1])
		return cmdreturn

	def addlun(self, port: str, ldev_id: str, **kwargs) -> object:

		cmdparam = self.cmdparam(port=port, ldev_id=ldev_id, **kwargs)
		if kwargs.get('lun_id'):
			lun_id = kwargs['lun_id']
			cmd = f"{self.path}raidcom add lun -port {port}{cmdparam} -ldev_id {ldev_id} -lun_id {kwargs['lun_id']} -I{self.instance} -s {self.serial}"
			
			#echo = f'echo "Executing: {undocmd}"'
			#self.undocmds.insert(0,undocmd)
			#self.undocmds.insert(0,echo)
			#cmdreturn.undocmds.insert(0,undocmd)
			
			cmdreturn = self.execute(cmd=cmd,**kwargs)
		else:
			cmd = f"{self.path}raidcom add lun -port {port}{cmdparam} -ldev_id {ldev_id} -I{self.instance} -s {self.serial}"
			cmdreturn = self.execute(cmd=cmd,**kwargs)
			lun = re.match('^raidcom: LUN \d+\((0x[0-9-af]+)\) will be used for adding',cmdreturn.stdout,re.I)
			if lun:
				lun_id = int(lun.group(1),16)
			else:
				raise Exception(f"Unable to extract lun information while mapping ldev_id {ldev_id} to {port}{cmdparam}")

		undocmd = f"{self.path}raidcom delete lun -port {port}{cmdparam} -ldev_id {ldev_id} -lun_id {lun_id} -I{self.instance} -s {self.serial}"
		echo = f'echo "Executing: {undocmd}"'
		self.undocmds.insert(0,undocmd)
		self.undocmds.insert(0,echo)
		cmdreturn.undocmds.insert(0,undocmd)    
		
		if not kwargs.get('noexec') and kwargs.get('return_lun'):
			getlun = self.getlun(port=f"{port}",lun_filter={ 'LUN': str(lun_id) },**kwargs)
			cmdreturn.data = getlun.data
			cmdreturn.view = getlun.view

		return cmdreturn

	def deletelun(self, port: str, ldev_id: str, lun_id: int='', host_grp_name: str='', gid: int='', **kwargs) -> object:
		cmd = f"{self.path}raidcom delete lun -port {port} {host_grp_name} -ldev_id {ldev_id} -lun_id {lun_id} -I{self.instance} -s {self.serial}"
		undocmd = [f"{self.path}raidcom add lun -port {port} {host_grp_name} -ldev_id {ldev_id} -lun_id {lun_id} -I{self.instance} -s {self.serial}"]
		undodef = [{'undodef':'addlun','args':{'port':port, 'host_grp_name':host_grp_name, 'ldev_id':ldev_id,'lun_id':lun_id}}]
		cmdreturn = self.execute(cmd,undocmd,undodef,**kwargs)
		return cmdreturn

	def unmapldev(self,ldev_id: str,virtual_ldev_id: str, **kwargs) -> object:
		cmd = f"{self.path}raidcom unmap resource -ldev_id {ldev_id} -virtual_ldev_id {virtual_ldev_id} -I{self.instance} -s {self.serial}"
		undocmd = [f"{self.path}raidcom map resource -ldev_id {ldev_id} -virtual_ldev_id {virtual_ldev_id} -I{self.instance} -s {self.serial}"]
		cmdreturn = self.execute(cmd,undocmd,**kwargs)
		return cmdreturn

	def mapldev(self,ldev_id: str,virtual_ldev_id: str, **kwargs) -> object:
		cmd = f"{self.path}raidcom map resource -ldev_id {ldev_id} -virtual_ldev_id {virtual_ldev_id} -I{self.instance} -s {self.serial}"
		undocmd = [f"{self.path}raidcom unmap resource -ldev_id {ldev_id} -virtual_ldev_id {virtual_ldev_id} -I{self.instance} -s {self.serial}"]
		cmdreturn = self.execute(cmd,undocmd,**kwargs)
		return cmdreturn

	def modifyldevname(self,ldev_id: str,ldev_name: str, **kwargs) -> object:
		cmd = f'{self.path}raidcom modify ldev -ldev_id {ldev_id} -ldev_name "{ldev_name}" -I{self.instance} -s {self.serial}'
		cmdreturn = self.execute(cmd,raidcom_asyncronous=False,**kwargs)
		return cmdreturn

	def commanddevice(self,ldev_id: str, security_level: int=0, **kwargs) -> object:
		'''
		0: Security: off, User Authentication: off, Group information acquisition: off
		1: Security: off, User Authentication: off, Group information acquisition: on
		2: Security: off, User Authentication: on,  Group information acquisition: off
		3: Security: off, User Authentication: on,  Group information acquisition: on
		4: Security: on,  User Authentication: off, Group information acquisition: off
		5: Security: on,  User Authentication: off, Group information acquisition: on
		6: Security: on,  User Authentication: on,  Group information acquisition: off
		7: Security: on,  User Authentication: on,  Group information acquisition: on
		'''
		cmd = f'{self.path}raidcom modify ldev -ldev_id {ldev_id} -command_device y {security_level} -I{self.instance} -s {self.serial}'
		cmdreturn = self.execute(cmd,raidcom_asyncronous=False,**kwargs)
		return cmdreturn

	def modifyldevcapacitysaving(self,ldev_id: str,capacity_saving: str, undo_saving: str="disable", **kwargs) -> object:
		'''
		Not fetching the previous capacity saving setting and defaulting to disable for undo.
		'''
		cmd = f"{self.path}raidcom modify ldev -ldev_id {ldev_id} -capacity_saving {capacity_saving} -I{self.instance} -s {self.serial}"
		undocmd = [f"{self.path}raidcom modify ldev -ldev_id {ldev_id} -capacity_saving {undo_saving} -I{self.instance} -s {self.serial}"]
		cmdreturn = self.execute(cmd,undocmd,raidcom_asyncronous=True,**kwargs)
		return cmdreturn
  
	def modifyhostgrp(self,port: str,host_mode: str, host_grp_name: str='', host_mode_opt: list=[], **kwargs) -> object:
		host_mode_opt_arg = ("",f"-set_host_mode_opt {' '.join(map(str,host_mode_opt))}")[len(host_mode_opt) > 0]
		cmd = f"{self.path}raidcom modify host_grp -port {port} {host_grp_name} -host_mode {host_mode} {host_mode_opt_arg} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		return cmdreturn

	def adddevicegrp(self, device_grp_name: str, device_name: str, ldev_id: str, **kwargs) -> object:
		cmd = f"{self.path}raidcom add device_grp -device_grp_name {device_grp_name} {device_name} -ldev_id {ldev_id} -I{self.instance} -s {self.serial}"
		undocmd = [f"{self.path}raidcom delete device_grp -device_grp_name {device_grp_name} {device_name} -ldev_id {ldev_id} -I{self.instance} -s {self.serial}"]
		cmdreturn = self.execute(cmd,undocmd,**kwargs)
		return cmdreturn

	def addcopygrp(self, copy_grp_name: str, device_grp_name: str, mirror_id: str=None, **kwargs) -> object:
		mirror_id_arg = ("",f"-mirror_id {mirror_id}")[mirror_id is not None] 
		cmd = f"{self.path}raidcom add copy_grp -copy_grp_name {copy_grp_name} -device_grp_name {device_grp_name} {mirror_id_arg} -I{self.instance} -s {self.serial}"
		undocmd = [f"{self.path}raidcom delete copy_grp -copy_grp_name {copy_grp_name} -device_grp_name {device_grp_name} -I{self.instance} -s {self.serial}"]
		cmdreturn = self.execute(cmd,undocmd,**kwargs)
		return cmdreturn

	def addhbawwn(self,port: str, hba_wwn: str, **kwargs) -> object:
		'''
		raidcom add hba_wwn\n
		examples:\n
		addhbawwn = Raidcom.addhbawwn(port="cl1-a-1")\n
		addhbawwn = Raidcom.addhbawwn(port="cl1-a",host_grp_name="MyHostGroup")\n
		addhbawwn = Raidcom.addhbawwn(port="cl1-a",gid=1)\n
		\n
		Returns Cmdview():\n
		addhbawwn.cmd\n
		addhbawwn.returncode\n
		addhbawwn.stderr\n
		addhbawwn.stdout\n
		'''
		cmdparam = self.cmdparam(port=port,**kwargs)
		cmd = f"{self.path}raidcom add hba_wwn -port {port}{cmdparam} -hba_wwn {hba_wwn} -I{self.instance} -s {self.serial}"
		undocmd = [f"{self.path}raidcom delete hba_wwn -port {port}{cmdparam} -hba_wwn {hba_wwn} -I{self.instance} -s {self.serial}"]
		cmdreturn = self.execute(cmd,undocmd,**kwargs)
		return cmdreturn

	def addwwnnickname(self,port: str, hba_wwn: str, wwn_nickname: str, **kwargs) -> object:
		'''
		Deprecated in favour of setwwnnickname
		'''
		cmdparam = self.cmdparam(port=port,**kwargs)
		cmd = f"{self.path}raidcom set hba_wwn -port {port}{cmdparam} -hba_wwn {hba_wwn} -wwn_nickname {wwn_nickname} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd)
		return cmdreturn

	def setwwnnickname(self,port: str, hba_wwn: str, wwn_nickname: str, **kwargs) -> object:
		'''
		raidcom set hba_wwn -port <port> [<host group name>] -hba_wwn <WWN strings> -wwn_nickname <WWN nickname>\n
		examples:\n
		setwwnnickname = Raidcom.setwwnnickname(port="cl1-a-1","hba_wwn":"1010101010101010","wwn_nickname":"BestWwnEver")\n
		setwwnnickname = Raidcom.setwwnnickname(port="cl1-a",host_grp_name="MyHostGroup","hba_wwn":"1010101010101010","wwn_nickname":"BestWwnEver")\n
		setwwnnickname = Raidcom.setwwnnickname(port="cl1-a",gid=1,host_grp_name="MyHostGroup","hba_wwn":"1010101010101010","wwn_nickname":"BestWwnEver")\n
		\n
		Returns Cmdview():\n
		setwwnnickname.cmd\n
		setwwnnickname.returncode\n
		setwwnnickname.stderr\n
		setwwnnickname.stdout\n
		'''
		cmdparam = self.cmdparam(port=port,**kwargs)
		cmd = f"{self.path}raidcom set hba_wwn -port {port}{cmdparam} -hba_wwn {hba_wwn} -wwn_nickname '{wwn_nickname}' -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd)
		return cmdreturn

	def gethostgrptcscan(self,port: str, gid: str=None, view_keyname='_replicationTC', **kwargs) -> object:

		if re.search(r'cl\w-\D+\d?-\d+',port,re.IGNORECASE):
			if gid: raise Exception('Fully qualified port requires no gid{}'.format(gid))
			cmdarg = ''
		else:
			if gid is None: raise Exception("raidscan requires gid if port is not fully qualified but it is set to none")
			cmdarg = "-"+str(gid)

		cmd = f"{self.path}raidscan -p {port}{cmdarg} -ITC{self.instance} -s {self.serial} -CLI"
		#getattr(self.parser,f"getpool_key_{key}")(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.gethostgrptcscan(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn

	def raidscanremote(self,port: str, gid=None, mode='TC', view_keyname='_remotereplication', **kwargs) -> object:
	
		if re.search(r'cl\w-\D+\d?-\d+',port,re.IGNORECASE):
			if gid: raise Exception('Fully qualified port requires no gid{}'.format(gid))
			cmdarg = ''
		else:
			if gid is None: raise Exception("raidscan requires gid if port is not fully qualified but it is set to none")
			cmdarg = "-"+str(gid)
		
		cmd = f"{self.path}raidscan -p {port}{cmdarg} -I{mode}{kwargs.get('instance',self.instance)} -s {self.serial} -CLI"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.raidscanremote(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn
	
	def raidscanmu(self,port,gid=None,mu=None,mode='',validmu=[0,1,2,3], view_keyname='_raidscanmu', parser='raidscanmu', **kwargs) -> object:
	
		if re.search(r'cl\w-\D+\d?-\d+',port,re.IGNORECASE):
			if gid: raise Exception('Fully qualified port requires no gid{}'.format(gid))
			cmdarg = ''
		else:
			if gid is None: raise Exception("raidscan requires gid if port is not fully qualified but it is set to none")
			cmdarg = "-"+str(gid)

		if mu == None or mu not in validmu: raise Exception("Please specify valid mu for raidscanmu")
		
		cmd = f"{self.path}raidscan -p {port}{cmdarg} -I{mode}{self.instance} -s {self.serial} -CLI -mu {mu}"
		cmdreturn = self.execute(cmd,**kwargs)
		getattr(self.parser,parser)(cmdreturn,mu)
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn

	def getrcu(self, view_keyname: str='_rcu', **kwargs) -> dict:
		cmd = f"{self.path}raidcom get rcu -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getrcu(cmdreturn)
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn

	def gethostgrprgid(self,port: str,resource_group_id: int, view_keyname='_ports', **kwargs) -> object:
		cmd = f"{self.path}raidcom get host_grp -port {port} -resource {resource_group_id} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.gethostgrprgid(cmdreturn,resource_group_id)
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn

	def gethostgrp_key_detail_rgid(self,port: str,resource_group_id: int, view_keyname='_ports', **kwargs) -> object:
		cmd = f"{self.path}raidcom get host_grp -port {port} -resource {resource_group_id} -key detail -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.gethostgrp_key_detail(cmdreturn)
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn

	def getquorum(self, view_keyname='_quorum', **kwargs) -> object:
		cmd = f"{self.path}raidcom get quorum -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getquorum(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		return cmdreturn

	def getdrive(self,view_keyname: str='_drives', update_view=True, **kwargs) -> object:
		'''
		raidcom get drive\n
		examples:\n
		drives = getparitygrp()\n
		drives = getparitygrp(datafilter={'R_TYPE':'14D+2P'})\n
		drives = getparitygrp(datafilter={'Anykey_when_val_is_callable':lambda a : a['DRIVE_TYPE'] != 'DKS5E-J900SS'})\n\n
		Returns Cmdview():\n
		parity_grps.serial\n
		parity_grps.data\n
		parity_grps.view\n
		parity_grps.cmd\n
		parity_grps.returncode\n
		parity_grps.stderr\n
		parity_grps.stdout\n
		parity_grps.stats\n
		'''
		cmd = f"{self.path}raidcom get drive -key opt -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		self.parser.getdrive(cmdreturn,datafilter=kwargs.get('datafilter',{}),**kwargs)
		if update_view:
			self.updateview(self.views,{view_keyname:cmdreturn.view})
			#self.updatestats.portcounters()
		
		return cmdreturn        

	def getnvmsubsystem(self, nvm_subsystem_id: int=None, view_keyname: str='_nvm', update_view=True, key=None, **kwargs) -> object:
		'''
		key = opt | optp | namespace | port | detail | undefined
		nvmsubsystem = getnvmsubsystem(key="detail",datafilter={'Anykey_when_val_is_callable':lambda a : a['SECURITY']) == "ENABLE"})\n
		'''
		#key_opt,attr = '',''
		key_opt = (f"-key {key}",'')[not key]

		cmd = f"{self.path}raidcom get nvm_subsystem {key_opt} -I{self.instance} -s {self.serial}"
		cmdreturn = self.execute(cmd,**kwargs)
		getattr(self.parser,f'getnvmsubsystem_key_{key}')(cmdreturn,datafilter=kwargs.get('datafilter',{}))

		if update_view:
			self.updateview(self.views,{view_keyname:{view_keyname:cmdreturn.view}})
			self.updatestats.nvmcounters()
			
		self.updatetimer(cmdreturn)    
		return cmdreturn


	def XXXXXXREFgetpool(self, key: str=None, view_keyname: str='_pools', **kwargs) -> object:
		'''
		pools = getpool()\n
		pools = getpool(datafilter={'POOL_NAME':'MyPool'})\n
		pools = getpool(datafilter={'Anykey_when_val_is_callable':lambda a : a['PT'] == 'HDT' or a['PT'] == 'HDP'})\n
		'''
		
		keyswitch = ("",f"-key {key}")[key is not None]
		cmd = f"{self.path}raidcom get pool -I{self.instance} -s {self.serial} {keyswitch}"
		cmdreturn = self.execute(cmd,**kwargs)
		getattr(self.parser,f"getpool_key_{key}")(cmdreturn,datafilter=kwargs.get('datafilter',{}))
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		self.updateview(self.data,{view_keyname:cmdreturn.data})
		self.updatestats.poolcounters()
		return cmdreturn    

	'''
	concurrent_{functions}
	'''
	def concurrent_zip(self,iterable):
		concurrent_instances = self.horcm_instance_list.copy()
		while len(concurrent_instances) < len(iterable):
			concurrent_instances.extend(self.horcm_instance_list.copy())
		return dict(zip(iterable, concurrent_instances))

	def update_concurrent_cmdreturn(self,cmdreturn,future):
		cmdreturn.stdout.append(future.result().stdout)
		cmdreturn.stderr.append(future.result().stderr)
		cmdreturn.data.extend(future.result().data)
		cmdreturn.cmds.append(future.result().cmd)
		cmdreturn.undocmds.extend(future.result().undocmds)
		cmdreturn.returncodes.append(future.result().returncode)
		cmdreturn.returncode += future.result().returncode
		self.updateview(cmdreturn.view,future.result().view)

	def concurrent_gethostgrps(self,ports: list=[], max_workers: int=30, view_keyname: str='_ports', **kwargs) -> object:
		'''
		host_grps = concurrent_gethostgrps(ports=['cl1-a','cl2-a'])\n
		host_grps = concurrent_gethostgrps(ports=['cl1-a','cl2-a'],datafilter={'HMD':'VMWARE_EX'})\n
		host_grps = concurrent_gethostgrps(port=['cl1-a','cl2-a'],datafilter={'GROUP_NAME':'MyGostGroup})\n
		host_grps = gethostgrp(port=['cl1-a','cl2-a'],datafilter={'Anykey_when_val_is_callable':lambda a : 'TEST' in a['GROUP_NAME'] })\n
		'''
		cmdreturn = CmdviewConcurrent()
		for port in ports: self.checkport(port)
		# With more horcm instances, we should be able to obtain our data more quickly.
		# To round robin available horcm instances, the instance list must be greater than 
		port_instances = self.concurrent_zip(ports)
		
		with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
			#future_out = { executor.submit(self.gethostgrp_key_detail,port=port,update_view=False,**kwargs): port for port in ports}
			future_out = { executor.submit(self.gethostgrp_key_detail,port=port,instance=port_instances[port],update_view=False,**kwargs): port for port in port_instances}
			for future in concurrent.futures.as_completed(future_out):
				self.update_concurrent_cmdreturn(cmdreturn,future)
				
		cmdreturn.serial = self.serial
		cmdreturn.view = dict(sorted(cmdreturn.view.items()))
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		self.updateview(self.data,{view_keyname:cmdreturn.data})
		self.updatestats.hostgroupcounters()
		self.updatetimer(cmdreturn)
		return cmdreturn

	def concurrent_gethbawwns(self,portgids: list=[], max_workers: int=30, view_keyname: str='_ports', **kwargs) -> object:
		''' e.g. \n
		ports=['cl1-a-3','cl1-a-4'] \n
		'''
		cmdreturn = CmdviewConcurrent()
		portgids_instances = self.concurrent_zip(portgids)
		
		for portgid in portgids: self.checkportgid(portgid)
		with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
			future_out = { executor.submit(self.gethbawwn,port=portgid,instance=portgids_instances[portgid],update_view=False,**kwargs): portgid for portgid in portgids_instances}
			for future in concurrent.futures.as_completed(future_out):
				self.update_concurrent_cmdreturn(cmdreturn,future)
		cmdreturn.serial = self.serial
		cmdreturn.view = dict(sorted(cmdreturn.view.items()))    
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		self.updateview(self.data,{view_keyname:cmdreturn.data})
		self.updatestats.hbawwncounters()
		self.updatetimer(cmdreturn)
		return cmdreturn

	def concurrent_getluns(self,portgids: list=[], max_workers: int=30, view_keyname: str='_ports', **kwargs) -> object:
		''' e.g. \n
		ports=['cl1-a-3','cl1-a-4'] \n
		'''
		cmdreturn = CmdviewConcurrent()
		for portgid in portgids: self.checkportgid(portgid)
		portgids_instances = self.concurrent_zip(portgids)
		with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
			future_out = { executor.submit(self.getlun,port=portgid,instance=portgids_instances[portgid],update_view=False,**kwargs): portgid for portgid in portgids_instances}
			for future in concurrent.futures.as_completed(future_out):
				self.update_concurrent_cmdreturn(cmdreturn,future)
		cmdreturn.serial = self.serial
		cmdreturn.view = dict(sorted(cmdreturn.view.items()))
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		self.updateview(self.data,{view_keyname:cmdreturn.data})
		self.updatestats.luncounters()
		self.updatetimer(cmdreturn)
		return cmdreturn

	def concurrent_getldevs(self,ldev_ids: list=[], max_workers: int=30, view_keyname: str='_ldevs', **kwargs) -> object:
		'''
		ldev_ids = [1234,1235,1236]\n
		'''
		cmdreturn = CmdviewConcurrent()
		ldevids_instances = self.concurrent_zip(ldev_ids)
		with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
			future_out = { executor.submit(self.getldev,ldev_id=ldev_id,instance=ldevids_instances[ldev_id],update_view=False,**kwargs): ldev_id for ldev_id in ldevids_instances}
			for future in concurrent.futures.as_completed(future_out):
				self.update_concurrent_cmdreturn(cmdreturn,future)
		cmdreturn.serial = self.serial
		cmdreturn.view = dict(sorted(cmdreturn.view.items()))
		
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		self.updatestats.ldevcounts()
		self.updatetimer(cmdreturn)
		return cmdreturn

	def concurrent_getportlogins(self,ports: list=[], max_workers: int=30, view_keyname: str='_ports', **kwargs) -> object:
		''' e.g. \n
		ports=['cl1-a','cl1-a'] \n
		'''
		cmdreturn = CmdviewConcurrent()
		for port in ports: self.checkport(port)
		port_instances = self.concurrent_zip(ports)
		with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
			future_out = { executor.submit(self.getportlogin,port=port,instance=port_instances[port],update_view=False,**kwargs): port for port in port_instances}
			for future in concurrent.futures.as_completed(future_out):
				self.update_concurrent_cmdreturn(cmdreturn,future)
		cmdreturn.serial = self.serial
		cmdreturn.view = dict(sorted(cmdreturn.view.items()))
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		self.updateview(self.data,{view_keyname:cmdreturn.data})
		self.updatestats.portlogincounters()
		self.updatetimer(cmdreturn)
		return cmdreturn

	def concurrent_raidscanremote(self,portgids: list=[], max_workers: int=30, view_keyname: str='_remotereplication', **kwargs) -> object:
		'''
		ldev_ids = [1234,1235,1236]\n
		mode='TC', view_keyname='_remotereplication',
		'''
		#def raidscanremote(self,port: str, gid=None, mode='TC', view_keyname='_remotereplication', **kwargs) -> object:
		cmdreturn = CmdviewConcurrent()
		for portgid in portgids: self.checkportgid(portgid)
		portgid_instances = self.concurrent_zip(portgids)
		with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
			future_out = { executor.submit(self.raidscanremote,port=portgid,instance=portgid_instances[portgid],update_view=False,**kwargs): portgid for portgid in portgid_instances}
			for future in concurrent.futures.as_completed(future_out):
				self.update_concurrent_cmdreturn(cmdreturn,future)
		cmdreturn.serial = self.serial
		cmdreturn.view = dict(sorted(cmdreturn.view.items()))
		
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		self.updatestats.ldevcounts()
		self.updatetimer(cmdreturn)
		return cmdreturn

	def concurrent_raidscanmuport(self,portgids: list=[], mus=[0,1,2,3], validmu=[0,1,2,3], max_workers: int=30, view_keyname: str='_remotereplication', **kwargs) -> object:
		'''
		portgids = ["cl1-a-1","cl1-a-2","cl3-a-4"]\n
		mode='TC', view_keyname='_remotereplication',
		'''
		#def raidscanremote(self,port: str, gid=None, mode='TC', view_keyname='_remotereplication', **kwargs) -> object:
		cmdreturn = CmdviewConcurrent()
		for portgid in portgids: self.checkportgid(portgid)
		portgid_instances = self.concurrent_zip(portgids)
		with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
			for mu in mus:
				future_out = { executor.submit(self.raidscanmu,port=portgid,mu=mu,instance=portgid_instances[portgid],update_view=False,view_keyname='_raidscanmuport',parser='raidscanmuport',**kwargs): portgid for portgid in portgid_instances}
				for future in concurrent.futures.as_completed(future_out):
					self.update_concurrent_cmdreturn(cmdreturn,future)
		cmdreturn.serial = self.serial
		cmdreturn.view = dict(sorted(cmdreturn.view.items()))
		
		self.updateview(self.views,{view_keyname:cmdreturn.view})
		self.updatestats.ldevcounts()
		self.updatetimer(cmdreturn)
		return cmdreturn



	def concurrent_addluns(self,lun_data: list=[{}], max_workers=20) -> object:
		'''
		lun_data: [{'PORT':CL1-A|CL1-A-1, 'GID':None|1, 'host_grp_name':'Name', 'LUN':0,'LDEV':1000}]
		
		lun_data = [
			{'PORT':'CL1-A', 'LUN':0, 'LDEV':46100, 'host_grp_name':'testing123'},
			{'PORT':'CL1-A', 'LUN':1, 'LDEV':46101, 'host_grp_name':'testing123'},
			{'PORT':'CL1-A', 'LUN':2, 'LDEV':46102, 'host_grp_name':'testing123'},
			{'PORT':'CL1-A', 'LUN':3, 'LDEV':46103, 'host_grp_name':'testing123'},
			{'PORT':'CL1-A', 'LUN':4, 'LDEV':46104, 'host_grp_name':'testing123'},
			{'PORT':'CL1-A', 'LUN':5, 'LDEV':46105, 'host_grp_name':'testing123'},
			{'PORT':'CL1-A', 'LUN':6, 'LDEV':46106, 'host_grp_name':'testing123'},
			{'PORT':'CL1-A', 'LUN':7, 'LDEV':46107, 'host_grp_name':'testing123'},
			{'PORT':'CL1-A', 'LUN':8, 'LDEV':46108, 'host_grp_name':'testing123'},
			{'PORT':'CL1-A', 'LUN':9, 'LDEV':46109, 'host_grp_name':'testing123'},
			{'PORT':'CL1-A', 'LUN':10, 'LDEV':46110, 'host_grp_name':'testing123'}]
		'''
		with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
			future_out = { executor.submit(self.addlun,port=lun['PORT'],lun_id=lun['LUN'],ldev_id=lun['LDEV'],host_grp_name=lun['host_grp_name']): lun for lun in lun_data}
			for future in concurrent.futures.as_completed(future_out):
				print(future.result().data)

	def concurrent_addldevs(self,ldev_data: list=[], return_ldevs: bool=True, max_workers=20) -> object:
		'''
		ldev_data = [
			{'LDEV|ldev_id':46115,'VOL_Capacity(BLK)|capacity':204800,'B_POOLID|poolid':0},
			{'LDEV|ldev_id':46116,'VOL_Capacity(BLK)|capacity':204800,'B_POOLID|poolid':0},
			{'LDEV|ldev_id':46117,'VOL_Capacity(BLK)|capacity':204800,'B_POOLID|poolid':0},
			{'LDEV|ldev_id':46118,'VOL_Capacity(BLK)|capacity':204800,'B_POOLID|poolid':0},
			{'LDEV|ldev_id':46119,'VOL_Capacity(BLK)|capacity':204800,'B_POOLID|poolid':0}
		]
		'''
		cmdreturn = CmdviewConcurrent()
		request_data = []
		for d in ldev_data:
			request_data.append({'ldev_id':d.get('LDEV',d.get('ldev_id')), 'capacity': d.get('VOL_Capacity(BLK)',d.get('capacity')), 'poolid':d.get('B_POOLID',d.get('poolid'))})
		
		with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
			future_out = { executor.submit(self.addldev,ldev_id=ldev['ldev_id'],capacity=ldev['capacity'],poolid=ldev['poolid'],return_ldev=return_ldevs): ldev for ldev in request_data}
			for future in concurrent.futures.as_completed(future_out):
				self.update_concurrent_cmdreturn(cmdreturn,future)
		cmdreturn.view = dict(sorted(cmdreturn.view.items()))
		return cmdreturn

	def addmfvvols(self, pool_name: str, num_ldevs: int=1, ldev_prefix: str="00:10:", initial_hex: str="0A", 
				 base_ldev_name: str="AUTO_LDEV", name_start: int=1, cylinder: int=10, 
				 emulation: str="3390-A", return_ldevs: bool=True, max_workers: int=10, **kwargs) -> object:
		"""Add multiple mainframe volumes with specified parameters.
		
		Args:
			pool_name (str): Name of the pool to create volumes in
			num_ldevs (int): Number of volumes to create
			ldev_prefix (str): Prefix for LDEV IDs (e.g., "00:10:")
			initial_hex (str): Starting hex value for the last part of the LDEV ID
			base_ldev_name (str): Base name for the LDEVs
			name_start (int): Starting number for the LDEV name suffix
			cylinder (int): Cylinder size for all volumes
			emulation (str): Emulation type for all volumes (e.g., "3390-A")
			return_ldevs (bool): Whether to return the created LDEVs
			max_workers (int): Maximum number of concurrent workers
			**kwargs: Additional arguments to pass to the command
			
		Returns:
			object: Command result containing the created LDEVs if return_ldevs is True
		"""
		def validate_cylinder(value):
			if not isinstance(value, int) or value <= 0:
				raise ValueError("Cylinder must be a positive integer")
			return value

		def validate_emulation(value):
			valid_emulations = ["3390-A", "3390-3", "3390-9", "3390-27", "3390-54", "3390-72"]
			if value not in valid_emulations:
				raise ValueError(f"Invalid emulation type. Must be one of: {', '.join(valid_emulations)}")
			return value

		# Validate inputs
		cylinder = validate_cylinder(cylinder)
		emulation = validate_emulation(emulation)

		# Create command return object
		cmdreturn = CmdviewConcurrent()
		cmdreturn.cmd = "addmfvvols"
		cmdreturn.data = []  # Initialize data attribute
		
		# Store successful creations and their names
		created_ldevs = {}

		# Step 1: Create volumes one by one to ensure success
		for i in range(num_ldevs):
			# Generate LDEV ID by incrementing the hex value
			current_hex = hex(int(initial_hex, 16) + i)[2:].upper().zfill(2)
			ldev_id = f"{ldev_prefix}{current_hex}"
			
			# Generate LDEV name with incremental number
			ldev_name = f"{base_ldev_name}{name_start + i}"
			
			# Create the LDEV using addvvolmf method
			try:
				result = self.addvvolmf(
					ldev_id=ldev_id,
					poolid=pool_name,
					cylinder=cylinder,
					emulation=emulation,
					return_ldev=False,
					**kwargs
				)
				
				# If successful, store the LDEV info for later name setting
				if result.returncode == 0:
					created_ldevs[ldev_id] = ldev_name
					
					# Update the command return view
					cmdreturn.view[ldev_id] = {
						'LDEV': ldev_id,
						'NAME': ldev_name,
						'POOL': pool_name,
						'CYLINDER': cylinder,
						'EMULATION': emulation
					}
					
					# Update command return with result
					cmdreturn.stdout += result.stdout + "\n"
					cmdreturn.stderr += result.stderr + "\n"
					
					self.log.info(f"Successfully created LDEV {ldev_id}")
				else:
					self.log.error(f"Failed to create LDEV {ldev_id}: {result.stderr}")
			except Exception as e:
				self.log.error(f"Error creating LDEV {ldev_id}: {str(e)}")
				continue
		
		# Step 2: Now set the names for each LDEV with retry mechanism
		# Wait for 2 seconds to allow storage system to fully process the LDEV creations
		import time
		time.sleep(2)
		
		for ldev_id, ldev_name in created_ldevs.items():
			# Try up to 3 times to set the name
			max_retries = 3
			retry_delay = 2  # seconds
			success = False
			
			for attempt in range(max_retries):
				try:
					# Construct direct command for naming
					cmd = f"{self.path}raidcom modify ldev -ldev_id {ldev_id} -ldev_name {ldev_name} -I{self.instance}"
					result = self.execute(cmd=cmd, **kwargs)
					
					if result.returncode == 0:
						self.log.info(f"Successfully set name '{ldev_name}' for LDEV {ldev_id}")
						success = True
						break
					else:
						# If error is that LDEV is not installed, wait and retry
						if "LDEV is not installed" in result.stderr:
							self.log.warning(f"LDEV {ldev_id} not fully registered yet, waiting {retry_delay} seconds before retry {attempt+1}/{max_retries}")
							time.sleep(retry_delay)
							# Increase delay for next retry
							retry_delay += 2
						else:
							self.log.error(f"Failed to set name for LDEV {ldev_id}: {result.stderr}")
							break
				except Exception as e:
					self.log.error(f"Error setting name for LDEV {ldev_id}: {str(e)}")
					break
			
			if not success:
				self.log.error(f"Failed to set name for LDEV {ldev_id} after {max_retries} attempts")
		
		# Step 3: Get LDEV details and populate the data attribute
		# Wait longer to ensure all operations have completed
		time.sleep(5)
		
		# Populate data attribute with information about all created LDEVs
		for ldev_id, ldev_name in created_ldevs.items():
			# Create a basic LDEV data entry with the information we already have
			ldev_data = {
				'LDEV_ID': ldev_id,
				'LDEV': ldev_id,
				'NAME': ldev_name,
				'POOL': pool_name,
				'CYLINDER': str(cylinder),
				'EMULATION': emulation,
				'TYPE': 'MF-VOL',
				'CUT': '-',
				'STATUS': 'NML'
			}
			
			# Try to get detailed information, but use our basic info if it fails
			if return_ldevs:
				try:
					# Get detailed information for this LDEV
					ldev_info = self.getldev(ldev_id=ldev_id, update_view=False, **kwargs)
					if hasattr(ldev_info, 'data') and ldev_info.data:
						# Use the detailed information
						ldev_data.update(ldev_info.data)
					elif hasattr(ldev_info, 'view') and ldev_id in ldev_info.view:
						# Add the view data
						detailed_data = ldev_info.view[ldev_id]
						ldev_data.update(detailed_data)
				except Exception as e:
					#self.log.warning(f"Could not get detailed info for LDEV {ldev_id}, using basic info: {str(e)}")
					# Don't log a warning - this is expected in some cases
					# Just use the basic info we already have
					self.log.debug(f"Using basic info for LDEV {ldev_id} - detailed info unavailable")
					pass
			
			# Add the LDEV data to our data list
			cmdreturn.data.append(ldev_data)
		
		# Return sorted view
		cmdreturn.view = dict(sorted(cmdreturn.view.items()))
		return cmdreturn

	def obfuscatepwd(self,cmd):
		if re.search(r' -login ',cmd):
			c = cmd.split()
			c[2] = "******"
			c[3] = "******"
			return ' '.join(c)
		else:
			return cmd
		
	def exception_string(self,cmdreturn):
		return json.dumps(ast.literal_eval(str(vars(cmdreturn))))

	def return_cci_exception(self,cmdreturn):
		try:
			errorcode = re.match(r".*\[(.*?)\].*",cmdreturn.stderr,re.DOTALL).group(1)
			if cci_exceptions_table.get(errorcode,{}).get('return_value',99999) == cmdreturn.returncode:
				cmdreturn.cci_error = errorcode
				return cci_exceptions_table[errorcode]['Exception']
		except:
			cmdreturn.cci_error = 'Unknown'
			return Exception
		
	def execute(self,cmd,undocmds=[],undodefs=[],expectedreturn=0,raise_err=True,**kwargs) -> object:

		cmdreturn = Cmdview(cmd=cmd)
		cmdreturn.expectedreturn = expectedreturn
		cmdreturn.serial = self.serial
		if kwargs.get('noexec'):
			return cmdreturn
		if kwargs.get('raidcom_asyncronous'):
			self.resetcommandstatus()
		self.log.debug(f"Executing: {self.obfuscatepwd(cmd)}")
		proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, shell=True)
		cmdreturn.stdout, cmdreturn.stderr = proc.communicate()
		cmdreturn.returncode = proc.returncode
		cmdreturn.executed = True

		if proc.returncode and proc.returncode != expectedreturn:
			self.log.error("Return > "+str(proc.returncode))
			self.log.error("Stdout > "+cmdreturn.stdout)
			self.log.error("Stderr > "+cmdreturn.stderr)
			if raise_err:
				raise self.return_cci_exception(cmdreturn)(self.exception_string(cmdreturn))
			
		for undocmd in undocmds: 
			echo = f'echo "Executing: {undocmd}"'
			self.undocmds.insert(0,undocmd)
			self.undocmds.insert(0,echo)
			cmdreturn.undocmds.insert(0,undocmd)
			
		for undodef in undodefs:
			self.undodefs.insert(0,undodef)
			cmdreturn.undodefs.insert(0,undodef)
		if self.cmdoutput:
			self.log.info(f"stdout: {cmdreturn.stdout}")
		if kwargs.get('raidcom_asyncronous'):
			self.getcommandstatus()


		
		return cmdreturn
		


	# BELOW IS OLD
	'''

	def pairevtwaitexec(self,cmd):
		self.log.info('Executing: {}'.format(cmd))
		proc = subprocess.Popen(cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
		return proc

	def restarthorcminst(self,inst):
		self.log.info('Restarting horcm instance {}'.format(inst))
		cmd = '{}horcmshutdown{} {}'.format(self.path,self.cciextension,inst)
		proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, shell=True)
		stdout, stderr = proc.communicate()
		if proc.returncode:
			if re.search(r'Can\'t be attached to HORC manager',stderr):
				self.log.warn('OK - Looks like horcm inst {} is already stopped'.format(inst))
			else:
				self.log.error("Return > "+str(proc.returncode))
				self.log.error("Stdout > "+stdout)
				self.log.error("Stderr > "+stderr)
				message = {'return':proc.returncode,'stdout':stdout, 'stderr':stderr }
				raise Exception('Unable to shutdown horcm inst: {}. Command dump > {}'.format(cmd,message))
				
		# Now start the instance
		time.sleep(2)
		cmd = '{}horcmstart{} {}'.format(self.path,self.cciextension,inst)
		proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, shell=True)
		stdout, stderr = proc.communicate()
		if proc.returncode:
			self.log.error("Return > "+str(proc.returncode))
			self.log.error("Stdout > "+stdout)
			self.log.error("Stderr > "+stderr)
			message = {'return':proc.returncode,'stdout':stdout, 'stderr':stderr }
			raise Exception('Unable to start horcm inst: {}. Command dump > {}'.format(cmd,message))




 

	# CCI
	def pairdisplay(self,inst: int,group: str,mode='',opts='',optviews: list=[]) -> dict:
		cmd = '{}pairdisplay -g {} -I{}{} {} -CLI'.format(self.path,group,mode,inst,opts)
		cmdreturn = self.execute(cmd)
		#cmdreturn['views'] = self.parser.pairdisplay(cmdreturn['stdout'],optviews)
		return cmdreturn
	
	def XXXpairvolchk(self,inst: int,group: str,device: str,expectedreturn: int):
		cmd = '{}pairvolchk -g {} -d {} -I{} -ss'.format(self.path,group,device,inst)
		cmdreturn = self.execute(cmd,expectedreturn=expectedreturn)
		return cmdreturn

	def pairvolchk(self,inst: int,group: str,device: str=None,expectedreturn: int=23,opts=''):
	
		check_device = ''
		if device:
			check_device = f'-d {device}'
		cmd = '{}pairvolchk -g {} {} -I{} -ss {}'.format(self.path,group,check_device,inst,opts)
		cmdreturn = self.execute(cmd,expectedreturn=expectedreturn)
		return cmdreturn

	def paircreate(self, inst: int, group: str, mode='', quorum='', jp='', js='', fence='', copy_pace=15):
		undocmd = []
		modifier = ''
		if re.search(r'\d',str(quorum)):
			modifier = '-jq {}'.format(quorum)
			undocmd.insert(0,'{}pairsplit -g {} -I{}{}'.format(self.path,group,mode,inst))
			undocmd.insert(0,'{}pairsplit -g {} -I{}{} -S'.format(self.path,group,mode,inst))

		if re.search(r'\d',str(jp)) and re.search(r'\d',str(js)):
			modifier = '-jp {} -js {}'.format(jp,js)
			undocmd.insert(0,'{}pairsplit -g {} -I{}{} -S'.format(self.path,group,mode,inst))

		cmd = '{}paircreate -g {} -vl {} -f {} -c {} -I{}{}'.format(self.path,group,modifier,fence,copy_pace,mode,inst)
		#self.log.info('Paircreate: {}'.format(cmd))
		
		cmdreturn = self.execute(cmd,undocmd)
		return cmdreturn

	def horctakeover(self, inst: int, group: str):
		cmd = '{}horctakeover -g {} -I{}'.format(self.path,group,inst)
		cmdreturn = self.execute(cmd,expectedreturn=1)
		return cmdreturn

	def pairresyncswaps(self, inst: int, group: str):
		cmd = '{}pairresync -swaps -g {} -I{}'.format(self.path,group,inst)
		cmdreturn = self.execute(cmd,expectedreturn=1)
		return cmdreturn

	def pairsplit(self, inst: int, group: str, opts=''):
		cmd = '{}pairsplit -g {} -I{} {}'.format(self.path,group,inst,opts)
		cmdreturn = self.execute(cmd)
		return cmdreturn
	
	def pairresync(self, inst: int, group: str, opts=''):
		cmd = '{}pairresync -g {} -I{} {}'.format(self.path,group,inst,opts)
		cmdreturn = self.execute(cmd)
		return cmdreturn
	def verbose(self,on=True):
		self.cmdoutput = on
	'''

	def addmfpvols(self, parity_grp_id: str, num_ldevs: int=1, ldev_prefix: str="00:10:", initial_hex: str="0A", 
				 base_ldev_name: str="AUTO_LDEV", name_start: int=1, cylinder: int=10, 
				 emulation: str="3390-A", mp_blade_id: str="0", format_ldevs: bool=True,
				 return_ldevs: bool=True, max_workers: int=10, **kwargs) -> object:
		"""Add multiple mainframe physical volumes with specified parameters.
		
		Args:
			parity_grp_id (str): ID of the parity group to create volumes in (e.g., "1-1")
			num_ldevs (int): Number of volumes to create
			ldev_prefix (str): Prefix for LDEV IDs (e.g., "00:10:")
			initial_hex (str): Starting hex value for the last part of the LDEV ID
			base_ldev_name (str): Base name for the LDEVs
			name_start (int): Starting number for the LDEV name suffix
			cylinder (int): Cylinder size for all volumes
			emulation (str): Emulation type for all volumes (e.g., "3390-A")
			mp_blade_id (str): MP blade ID for the volumes
			format_ldevs (bool): Whether to quick format the LDEVs after creation
			return_ldevs (bool): Whether to return the created LDEVs
			max_workers (int): Maximum number of concurrent workers
			**kwargs: Additional arguments to pass to the command
			
		Returns:
			object: Command result containing the created LDEVs if return_ldevs is True
		"""
		def validate_cylinder(value):
			if not isinstance(value, int) or value <= 0:
				raise ValueError("Cylinder must be a positive integer")
			return value

		def validate_emulation(value):
			valid_emulations = ["3390-A", "3390-3", "3390-9", "3390-27", "3390-54", "3390-72", "3390-V"]
			if value not in valid_emulations:
				raise ValueError(f"Invalid emulation type. Must be one of: {', '.join(valid_emulations)}")
			return value

		# Validate inputs
		cylinder = validate_cylinder(cylinder)
		emulation = validate_emulation(emulation)

		# Create command return object
		cmdreturn = CmdviewConcurrent()
		cmdreturn.cmd = "addmfpvols"
		cmdreturn.data = []  # Initialize data attribute
		
		# Store successful creations and their names
		created_ldevs = {}

		# Step 1: Create volumes one by one to ensure success
		for i in range(num_ldevs):
			# Generate LDEV ID by incrementing the hex value
			current_hex = hex(int(initial_hex, 16) + i)[2:].upper().zfill(2)
			ldev_id = f"{ldev_prefix}{current_hex}"
			
			# Generate LDEV name with incremental number
			ldev_name = f"{base_ldev_name}{name_start + i}"
			
			# Create the LDEV using parity group
			try:
				cmd = f"{self.path}raidcom add ldev -parity_grp_id {parity_grp_id} -ldev_id {ldev_id} -cylinder {cylinder} -emulation {emulation} -mp_blade_id {mp_blade_id} -I{self.instance}"
				result = self.execute(cmd=cmd, **kwargs)
				
				# If successful, store the LDEV info for later name setting and formatting
				if result.returncode == 0:
					created_ldevs[ldev_id] = ldev_name
					
					# Update the command return view
					cmdreturn.view[ldev_id] = {
						'LDEV': ldev_id,
						'NAME': ldev_name,
						'PARITY_GRP': parity_grp_id,
						'CYLINDER': cylinder,
						'EMULATION': emulation,
						'MP_BLADE_ID': mp_blade_id
					}
					
					# Update command return with result
					cmdreturn.stdout += result.stdout + "\n"
					cmdreturn.stderr += result.stderr + "\n"
					
					self.log.info(f"Successfully created LDEV {ldev_id}")
				else:
					self.log.error(f"Failed to create LDEV {ldev_id}: {result.stderr}")
			except Exception as e:
				self.log.error(f"Error creating LDEV {ldev_id}: {str(e)}")
				continue
		
		# Step 2: Check command status and reset
		try:
			self.getcommandstatus()
			self.resetcommandstatus()
		except Exception as e:
			self.log.error(f"Error checking/resetting command status: {str(e)}")
		
		# Step 3: Now set the names for each LDEV with retry mechanism
		# Wait for 2 seconds to allow storage system to fully process the LDEV creations
		import time
		time.sleep(2)
		
		for ldev_id, ldev_name in created_ldevs.items():
			# Try up to 3 times to set the name
			max_retries = 3
			retry_delay = 2  # seconds
			success = False
			
			for attempt in range(max_retries):
				try:
					# Construct direct command for naming
					cmd = f"{self.path}raidcom modify ldev -ldev_id {ldev_id} -ldev_name {ldev_name} -I{self.instance}"
					result = self.execute(cmd=cmd, **kwargs)
					
					if result.returncode == 0:
						self.log.info(f"Successfully set name '{ldev_name}' for LDEV {ldev_id}")
						success = True
						break
					else:
						# If error is that LDEV is not installed, wait and retry
						if "LDEV is not installed" in result.stderr:
							self.log.warning(f"LDEV {ldev_id} not fully registered yet, waiting {retry_delay} seconds before retry {attempt+1}/{max_retries}")
							time.sleep(retry_delay)
							# Increase delay for next retry
							retry_delay += 2
						else:
							self.log.error(f"Failed to set name for LDEV {ldev_id}: {result.stderr}")
							break
				except Exception as e:
					self.log.error(f"Error setting name for LDEV {ldev_id}: {str(e)}")
					break
			
			if not success:
				self.log.error(f"Failed to set name for LDEV {ldev_id} after {max_retries} attempts")
		  
		# Step 4: Quick format the LDEVs if requested
		time.sleep(2)
		if format_ldevs and created_ldevs:
			for ldev_id in created_ldevs:
				for attempt in range(3):
					try:
						cmd = f"{self.path}raidcom initialize ldev -ldev_id {ldev_id} -operation qfmt -I{self.instance}"
						result = self.execute(cmd=cmd, **kwargs)
						
						if result.returncode == 0:
							self.log.info(f"Successfully started quick format for LDEV {ldev_id}")
							break
						else:
							self.log.warning(f"Retrying quick format for LDEV {ldev_id} (attempt {attempt + 1})")
							time.sleep(3)
							# self.log.error(f"Failed to start quick format for LDEV {ldev_id}: {result.stderr}")
					except Exception as e:
						self.log.error(f"Error during quick format for LDEV {ldev_id}: {str(e)}")
				# except Exception as e:
				# 	self.log.error(f"Error during quick format for LDEV {ldev_id}: {str(e)}")
		
		# Step 5: Get LDEV details and populate the data attribute
		# Wait longer to ensure all operations have completed
		time.sleep(5)
		
		# Populate data attribute with information about all created LDEVs
		for ldev_id, ldev_name in created_ldevs.items():
			# Create a basic LDEV data entry with the information we already have
			ldev_data = {
				'LDEV_ID': ldev_id,
				'LDEV': ldev_id,
				'NAME': ldev_name,
				'PARITY_GRP': parity_grp_id,
				'CYLINDER': str(cylinder),
				'EMULATION': emulation,
				'MP_BLADE_ID': mp_blade_id,
				'TYPE': 'MF-PVOL',
				'CUT': '-',
				'STATUS': 'NML'
			}
			
			# Try to get detailed information, but use our basic info if it fails
			if return_ldevs:
				try:
					# Get detailed information for this LDEV
					ldev_info = self.getldev(ldev_id=ldev_id, update_view=False, **kwargs)
					if hasattr(ldev_info, 'data') and ldev_info.data:
						# Use the detailed information
						ldev_data.update(ldev_info.data)
					elif hasattr(ldev_info, 'view') and ldev_id in ldev_info.view:
						# Add the view data
						detailed_data = ldev_info.view[ldev_id]
						ldev_data.update(detailed_data)
				except Exception as e:
					# Don't log a warning - this is expected in some cases
					self.log.debug(f"Using basic info for LDEV {ldev_id} - detailed info unavailable")
					pass
			
			# Add the LDEV data to our data list
			cmdreturn.data.append(ldev_data)
		
		# Return sorted view
		cmdreturn.view = dict(sorted(cmdreturn.view.items()))
		return cmdreturn
		
	def addmfdppool(self, pool_id: int, pool_name: str, ldev_id: Union[int, str, list], 
					cnt: str=None, grp_opt: str=None, device_grp_name: str=None, 
					user_threshold: str=None, return_pool: bool=True, **kwargs) -> object:
		"""Add a dynamic provisioning (DP) pool.
		
		Args:
			pool_id (int): ID of the pool to create
			pool_name (str): Name for the pool
			ldev_id (Union[int, str, list]): LDEV ID(s) to use for the pool
			cnt (str, optional): Count (2 to 64)
			grp_opt (str, optional): Group option
			device_grp_name (str, optional): Device group name or device name
			user_threshold (str, optional): User threshold values (e.g., "70 80")
			return_pool (bool): Whether to return pool details
			**kwargs: Additional arguments to pass to the command
			
		Returns:
			object: Command result containing the created pool if return_pool is True
		"""
		# Create command return object
		cmdreturn = Cmdview(cmd="addmfdppool")
		cmdreturn.data = []  # Initialize data attribute
		
		# Reset command status first
		try:
			self.resetcommandstatus()
		except Exception as e:
			self.log.error(f"Error resetting command status: {str(e)}")
		
		# Build the base command
		cmd = f"{self.path}raidcom add dp_pool -pool_id {pool_id} -pool_name {pool_name}"
		
		# Handle LDEV IDs
		if isinstance(ldev_id, list):
			ldev_ids = ' '.join(str(ldev) for ldev in ldev_id)
			cmd += f" -ldev_id {ldev_ids}"
		else:
			cmd += f" -ldev_id {ldev_id}"
		
		# Add optional parameters if provided
		if cnt:
			cmd += f" -cnt {cnt}"
		if grp_opt:
			cmd += f" -grp_opt {grp_opt}"
		if device_grp_name:
			cmd += f" -device_grp_name {device_grp_name}"
		if user_threshold:
			cmd += f" -user_threshold {user_threshold}"
		
		# Add instance ID
		cmd += f" -I{self.instance}"
		
		# Execute the command
		try:
			result = self.execute(cmd=cmd, **kwargs)
			
			# Update command return with result
			cmdreturn.stdout = result.stdout
			cmdreturn.stderr = result.stderr
			cmdreturn.returncode = result.returncode
			
			# Update command view with basic info
			basic_pool_info = {
				'POOL_ID': str(pool_id),
				'POOL_NAME': pool_name,
				'LDEV_ID': str(ldev_id) if not isinstance(ldev_id, list) else ','.join(str(ldev) for ldev in ldev_id),
			}
			
			if result.returncode == 0:
				self.log.info(f"Successfully created DP pool {pool_id} with name '{pool_name}'")
				cmdreturn.view[str(pool_id)] = basic_pool_info
				
				# Add the pool info to the data attribute
				pool_data = basic_pool_info.copy()
				if cnt:
					pool_data['CNT'] = cnt
				if grp_opt:
					pool_data['GRP_OPT'] = grp_opt
				if device_grp_name:
					pool_data['DEVICE_GRP_NAME'] = device_grp_name
				if user_threshold:
					pool_data['USER_THRESHOLD'] = user_threshold
				
				cmdreturn.data.append(pool_data)
				
				# If return_pool is True, get detailed pool information
				if return_pool:
					try:
						# Wait to ensure pool is fully created
						import time
						time.sleep(2)
						
						# Get detailed pool information
						pool_info = self.getdppool(pool_id=pool_id, **kwargs)
						
						if hasattr(pool_info, 'data') and pool_info.data:
							# Replace our basic data with detailed info
							cmdreturn.data = pool_info.data
						elif hasattr(pool_info, 'view') and str(pool_id) in pool_info.view:
							# Use the view data
							detailed_data = pool_info.view[str(pool_id)]
							cmdreturn.data = [detailed_data]
					except Exception as e:
						self.log.debug(f"Using basic info for pool {pool_id} - detailed info unavailable: {str(e)}")
			else:
				self.log.error(f"Failed to create DP pool {pool_id}: {result.stderr}")
		except Exception as e:
			self.log.error(f"Error creating DP pool {pool_id}: {str(e)}")
		
		# Check and get command status
		try:
			self.getcommandstatus()
			self.resetcommandstatus()
		except Exception as e:
			self.log.error(f"Error checking/resetting command status: {str(e)}")
		
		return cmdreturn
		
	def addmultipleopenvvols(self, poolid: int, capacity: Union[int, str]=2097152, 
				 ldev_prefix: str=None, initial_hex: str=None, base_ldev_name: str="OPEN_VVOL", 
				 name_start: int=1, emulation: str="OPEN-V", start_ldev: int=None, end_ldev: int=None,
				 return_ldevs: bool=True, max_workers: int=10, **kwargs) -> object:
		"""Add multiple open system virtual volumes with specified parameters.
		
		Args:
			poolid (int): ID of the pool to create volumes in
			capacity (Union[int, str]): Capacity in blocks or size with unit (e.g., "1g", "100m")
			ldev_prefix (str, optional): Prefix for LDEV IDs when not using auto assignment
			initial_hex (str, optional): Starting hex value for the last part of the LDEV ID when not using auto
			base_ldev_name (str): Base name for the LDEVs
			name_start (int): Starting number for the LDEV name suffix
			emulation (str): Emulation type for all volumes (e.g., "OPEN-V")
			start_ldev (int): Start of LDEV ID range to search if using auto assignment
			end_ldev (int): End of LDEV ID range to search if using auto assignment
			return_ldevs (bool): Whether to return the created LDEVs
			max_workers (int): Maximum number of concurrent workers
			**kwargs: Additional arguments to pass to the command
			
		Returns:
			object: Command result containing the created LDEVs if return_ldevs is True
		"""
		def validate_emulation(value):
			valid_emulations = ["OPEN-V", "OPEN-3", "OPEN-8", "OPEN-9", "OPEN-K", "OPEN-L", "OPEN-E"]
			if value not in valid_emulations:
				raise ValueError(f"Invalid emulation type. Must be one of: {', '.join(valid_emulations)}")
			return value
			
		def convert_capacity(cap_value):
			"""Convert capacity with units to blocks if needed"""
			if isinstance(cap_value, str) and not cap_value.isdigit():
				units = {
					'K': 1024 / 512, 
					'M': 1024**2 / 512, 
					'G': 1024**3 / 512, 
					'T': 1024**4 / 512, 
					'P': 1024**5 / 512
				}
				
				unit = cap_value[-1].upper()
				if unit in units:
					try:
						value = float(cap_value[:-1])
						return str(int(value * units[unit]))
					except ValueError:
						pass
				
				# Try with KB, MB, etc. format
				if len(cap_value) > 2:
					unit2 = cap_value[-2:].upper()
					if unit2 in ['KB', 'MB', 'GB', 'TB', 'PB']:
						try:
							value = float(cap_value[:-2])
							return str(int(value * units[unit2[0]]))
						except ValueError:
							pass
			
			return str(cap_value)  # Return as-is if not convertible

		# Validate emulation type
		try:
			emulation = validate_emulation(emulation)
		except ValueError as e:
			if self.asyncmode:
				cmdreturn = Cmdview(cmd="addmultipleopenvvols")
				cmdreturn.returncode = 999
				cmdreturn.stderr = str(e)
				return cmdreturn
			raise

		# Create command return object
		cmdreturn = CmdviewConcurrent()
		cmdreturn.cmd = "addmultipleopenvvols"
		cmdreturn.data = []  # Initialize data attribute
		
		# Convert capacity if it has units
		capacity_blocks = convert_capacity(capacity)
		
		# Determine method for LDEV creation
		use_auto_ldev = (start_ldev is not None and end_ldev is not None)
		use_range_prefix = (ldev_prefix is not None and initial_hex is not None)
		
		if not use_auto_ldev and not use_range_prefix:
			err_msg = "Either (start_ldev and end_ldev) or (ldev_prefix and initial_hex) must be provided"
			if self.asyncmode:
				cmdreturn.returncode = 999
				cmdreturn.stderr = err_msg
				return cmdreturn
			raise ValueError(err_msg)
		
		# Calculate max number of volumes to create based on either range or manual prefix
		if use_auto_ldev:
			max_ldevs = end_ldev - start_ldev + 1
			self.log.info(f"Will try to create up to {max_ldevs} volumes in range {start_ldev}-{end_ldev}")
		else:
			# If not auto, we rely on the range provided by initial_hex
			# For now, limit to a reasonable number to prevent too many attempts
			max_ldevs = 20  # Arbitrary limit for manual range
			self.log.info(f"Will try to create volumes using prefix {ldev_prefix} starting with hex {initial_hex}")
		 
		# Track which LDEVs were attempted but unavailable
		unavailable_ldevs = []
		created_ldevs = {}
		
		# Step 1: Create volumes based on the range
		for i in range(max_ldevs):
			try:
				# Determine LDEV ID based on method
				if use_auto_ldev:
					# For auto LDEV assignment, we send the range and let the storage system choose
					# But we also keep track of how many we've attempted to create to honor max_ldevs
					
					# Create command with auto-assignment from the range
					cmd = f"{self.path}raidcom add ldev -ldev_id auto -request_id auto -ldev_range {start_ldev}-{end_ldev} -pool {poolid} -capacity {capacity_blocks} -emulation {emulation} -I{self.instance} -s {self.serial}"
					result = self.execute(cmd, **kwargs)
					
					if result.returncode != 0:
						err_msg = f"Failed to create VVOL in range {start_ldev}-{end_ldev}: {result.stderr}"
						self.log.error(err_msg)
						
						# Check if we've exhausted the available LDEVs in the range
						if "LDEV is already defined" in result.stderr or "All specified LDEVs are already assigned to volumes" in result.stderr:
							self.log.warning(f"No more available LDEVs in range {start_ldev}-{end_ldev}")
							break
						
						# For other errors, let's try the next attempt
						continue
					
					# Get the request ID
					reqid = result.stdout.rstrip().split(' : ')
					if not re.search(r'REQID', reqid[0]):
						err_msg = f"Unable to obtain REQID from stdout {result}"
						self.log.error(err_msg)
						continue
					
					# Get the automatically assigned LDEV ID
					try:
						cmd_status = self.getcommandstatus(request_id=reqid[1])
						self.parser.getcommandstatus(cmd_status)
						auto_ldev_id = cmd_status.data[0]['ID']
						self.resetcommandstatus(request_id=reqid[1])
						ldev_id = auto_ldev_id
						
						# Track which LDEV was used to avoid trying it again
						if start_ldev <= int(ldev_id) <= end_ldev:
							# Mark this specific ID as used by adding it to unavailable_ldevs
							unavailable_ldevs.append(ldev_id)
					except Exception as e:
						self.log.error(f"Error getting command status: {str(e)}")
						continue
				else:
					# Generate LDEV ID by incrementing the hex value
					current_hex = hex(int(initial_hex, 16) + i)[2:].upper().zfill(2)
					ldev_id = f"{ldev_prefix}{current_hex}"
					
					# Create the LDEV with specific ID
					cmd = f"{self.path}raidcom add ldev -ldev_id {ldev_id} -pool {poolid} -capacity {capacity_blocks} -emulation {emulation} -I{self.instance} -s {self.serial}"
					result = self.execute(cmd, **kwargs)
					
					if result.returncode != 0:
						self.log.error(f"Failed to create LDEV {ldev_id}: {result.stderr}")
						unavailable_ldevs.append(ldev_id)
						continue
				
				# Generate LDEV name with incremental number
				ldev_name = f"{base_ldev_name}_{name_start + i}"
				created_ldevs[ldev_id] = ldev_name
				
				# Update the command return view
				cmdreturn.view[ldev_id] = {
					'LDEV': ldev_id,
					'NAME': ldev_name,
					'POOL': str(poolid),
					'CAPACITY': capacity_blocks,
					'EMULATION': emulation
				}
				
				# Update command return with result
				cmdreturn.stdout += result.stdout + "\n" if hasattr(result, 'stdout') else ""
				cmdreturn.stderr += result.stderr + "\n" if hasattr(result, 'stderr') else ""
				
				self.log.info(f"Successfully created LDEV {ldev_id}")
				
			except Exception as e:
				self.log.error(f"Error creating LDEV: {str(e)}")
				continue
		
		# Report what we were able to create
		self.log.info(f"Created {len(created_ldevs)} volumes out of {max_ldevs} maximum ({len(unavailable_ldevs)} unavailable)")
		
		# Step 2: Now set the names for each LDEV with retry mechanism
		# Wait for 2 seconds to allow storage system to fully process the LDEV creations
		# import time
		time.sleep(2)
		
		for ldev_id, ldev_name in created_ldevs.items():
			# Try up to 3 times to set the name
			max_retries = 3
			retry_delay = 2  # seconds
			success = False
			
			for attempt in range(max_retries):
				try:
					# Construct direct command for naming
					cmd = f"{self.path}raidcom modify ldev -ldev_id {ldev_id} -ldev_name {ldev_name} -I{self.instance}"
					result = self.execute(cmd=cmd, **kwargs)
					
					if result.returncode == 0:
						self.log.info(f"Successfully set name '{ldev_name}' for LDEV {ldev_id}")
						success = True
						break
					else:
						# If error is that LDEV is not installed, wait and retry
						if "LDEV is not installed" in result.stderr:
							self.log.warning(f"LDEV {ldev_id} not fully registered yet, waiting {retry_delay} seconds before retry {attempt+1}/{max_retries}")
							time.sleep(retry_delay)
							# Increase delay for next retry
							retry_delay += 2
						else:
							self.log.error(f"Failed to set name for LDEV {ldev_id}: {result.stderr}")
							break
				except Exception as e:
					self.log.error(f"Error setting name for LDEV {ldev_id}: {str(e)}")
					break
			
			if not success:
				self.log.error(f"Failed to set name for LDEV {ldev_id} after {max_retries} attempts")
		
		# Step 3: Get LDEV details and populate the data attribute
		# Wait longer to ensure all operations have completed
		time.sleep(3)
		
		# Store information about unavailable LDEVs
		cmdreturn.unavailable_ldevs = unavailable_ldevs
		
		# Populate data attribute with information about all created LDEVs
		for ldev_id, ldev_name in created_ldevs.items():
			# Create a basic LDEV data entry with the information we already have
			ldev_data = {
				'LDEV': ldev_id,
				'NAME': ldev_name,
				'POOL': str(poolid),
				'CAPACITY': capacity_blocks,
				'EMULATION': emulation,
				'TYPE': 'OPEN-VVOL',
				'STATUS': 'NML'
			}
			
			# Try to get detailed information, but use our basic info if it fails
			if return_ldevs:
				try:
					# Get detailed information for this LDEV
					ldev_info = self.getldev(ldev_id=ldev_id, update_view=False, **kwargs)
					if hasattr(ldev_info, 'data') and ldev_info.data:
						# Use the detailed information
						ldev_data.update(ldev_info.data[0])
					elif hasattr(ldev_info, 'view') and ldev_id in ldev_info.view:
						# Add the view data
						detailed_data = ldev_info.view[ldev_id]
						ldev_data.update(detailed_data)
				except Exception as e:
					self.log.debug(f"Using basic info for LDEV {ldev_id} - detailed info unavailable")
					pass
			
			# Add the LDEV data to our data list
			cmdreturn.data.append(ldev_data)
		
		# Return sorted view
		cmdreturn.view = dict(sorted(cmdreturn.view.items()))
		
		# Add a summary of what was created vs what was attempted
		cmdreturn.summary = {
			'max_ldevs': max_ldevs,
			'created_ldevs': len(created_ldevs),
			'unavailable_ldevs': len(unavailable_ldevs)
		}
		
		return cmdreturn
		
	def addmultipleopenpvols(self, parity_grp_id: str, capacity: Union[int, str]=2097152, 
				 ldev_prefix: str=None, initial_hex: str=None, base_ldev_name: str="OPEN_PVOL", 
				 name_start: int=1, emulation: str="OPEN-V", start_ldev: int=None, end_ldev: int=None,
				 mp_blade_id: str="0", format_ldevs: bool=True, 
				 return_ldevs: bool=True, max_workers: int=10, **kwargs) -> object:
		"""Add multiple open system physical volumes with specified parameters.
		
		Args:
			parity_grp_id (str): ID of the parity group to create volumes in (e.g., "1-1")
			capacity (Union[int, str]): Capacity in blocks or size with unit (e.g., "1g", "100m")
			ldev_prefix (str, optional): Prefix for LDEV IDs when not using auto assignment
			initial_hex (str, optional): Starting hex value for the last part of the LDEV ID when not using auto
			base_ldev_name (str): Base name for the LDEVs
			name_start (int): Starting number for the LDEV name suffix
			emulation (str): Emulation type for all volumes (e.g., "OPEN-V")
			start_ldev (int): Start of LDEV ID range to search if using auto assignment
			end_ldev (int): End of LDEV ID range to search if using auto assignment
			mp_blade_id (str): MP blade ID for the volumes
			format_ldevs (bool): Whether to quick format the LDEVs after creation
			return_ldevs (bool): Whether to return the created LDEVs
			max_workers (int): Maximum number of concurrent workers
			**kwargs: Additional arguments to pass to the command
			
		Returns:
			object: Command result containing the created LDEVs if return_ldevs is True
		"""
		def validate_emulation(value):
			valid_emulations = ["OPEN-V", "OPEN-3", "OPEN-8", "OPEN-9", "OPEN-K", "OPEN-L", "OPEN-E"]
			if value not in valid_emulations:
				raise ValueError(f"Invalid emulation type. Must be one of: {', '.join(valid_emulations)}")
			return value
			
		def convert_capacity(cap_value):
			"""Convert capacity with units to blocks if needed"""
			if isinstance(cap_value, str) and not cap_value.isdigit():
				units = {
					'K': 1024 / 512, 
					'M': 1024**2 / 512, 
					'G': 1024**3 / 512, 
					'T': 1024**4 / 512, 
					'P': 1024**5 / 512
				}
				
				unit = cap_value[-1].upper()
				if unit in units:
					try:
						value = float(cap_value[:-1])
						return str(int(value * units[unit]))
					except ValueError:
						pass
				
				# Try with KB, MB, etc. format
				if len(cap_value) > 2:
					unit2 = cap_value[-2:].upper()
					if unit2 in ['KB', 'MB', 'GB', 'TB', 'PB']:
						try:
							value = float(cap_value[:-2])
							return str(int(value * units[unit2[0]]))
						except ValueError:
							pass
			
			return str(cap_value)  # Return as-is if not convertible

		# Validate emulation type
		try:
			emulation = validate_emulation(emulation)
		except ValueError as e:
			if self.asyncmode:
				cmdreturn = Cmdview(cmd="addmultipleopenpvols")
				cmdreturn.returncode = 999
				cmdreturn.stderr = str(e)
				return cmdreturn
			raise

		# Create command return object
		cmdreturn = CmdviewConcurrent()
		cmdreturn.cmd = "addmultipleopenpvols"
		cmdreturn.data = []  # Initialize data attribute
		
		# Convert capacity if it has units
		capacity_blocks = convert_capacity(capacity)
		
		# Determine method for LDEV creation
		use_auto_ldev = (start_ldev is not None and end_ldev is not None)
		use_range_prefix = (ldev_prefix is not None and initial_hex is not None)
		
		if not use_auto_ldev and not use_range_prefix:
			err_msg = "Either (start_ldev and end_ldev) or (ldev_prefix and initial_hex) must be provided"
			if self.asyncmode:
				cmdreturn.returncode = 999
				cmdreturn.stderr = err_msg
				return cmdreturn
			raise ValueError(err_msg)
		
		# Calculate max number of volumes to create based on either range or manual prefix
		if use_auto_ldev:
			max_ldevs = end_ldev - start_ldev + 1
			self.log.info(f"Will try to create up to {max_ldevs} physical volumes in range {start_ldev}-{end_ldev}")
		else:
			# If not auto, we rely on the range provided by initial_hex
			# For now, limit to a reasonable number to prevent too many attempts
			max_ldevs = 20  # Arbitrary limit for manual range
			self.log.info(f"Will try to create physical volumes using prefix {ldev_prefix} starting with hex {initial_hex}")
		
		# Track which LDEVs were attempted but unavailable
		unavailable_ldevs = []
		created_ldevs = {}
		
		# Step 1: Create volumes based on the range
		for i in range(max_ldevs):
			try:
				# Determine LDEV ID based on method
				if use_auto_ldev:
					# For auto LDEV assignment, we calculate the next available LDEV in the range
					current_ldev = start_ldev + i
					if current_ldev > end_ldev:
						self.log.warning(f"Reached end of specified LDEV range ({start_ldev}-{end_ldev})")
						break
						
					# Check if this LDEV is already in unavailable_ldevs
					if str(current_ldev) in unavailable_ldevs:
						continue
						
					ldev_id = str(current_ldev)
					
					# Create the LDEV with parity group
					cmd = f"{self.path}raidcom add ldev -parity_grp_id {parity_grp_id} -ldev_id {ldev_id} -capacity {capacity_blocks} -emulation {emulation} -mp_blade_id {mp_blade_id} -I{self.instance} -s {self.serial}"
					result = self.execute(cmd, **kwargs)
					
					if result.returncode != 0:
						err_msg = f"Failed to create PVOL with ID {ldev_id} in parity group {parity_grp_id}: {result.stderr}"
						self.log.error(err_msg)
						unavailable_ldevs.append(str(ldev_id))
						continue
				else:
					# Generate LDEV ID by incrementing the hex value
					current_hex = hex(int(initial_hex, 16) + i)[2:].upper().zfill(2)
					ldev_id = f"{ldev_prefix}{current_hex}"
					
					# Create the LDEV with parity group
					cmd = f"{self.path}raidcom add ldev -parity_grp_id {parity_grp_id} -ldev_id {ldev_id} -capacity {capacity_blocks} -emulation {emulation} -mp_blade_id {mp_blade_id} -I{self.instance} -s {self.serial}"
					result = self.execute(cmd, **kwargs)
					
					if result.returncode != 0:
						self.log.error(f"Failed to create PVOL {ldev_id}: {result.stderr}")
						unavailable_ldevs.append(ldev_id)
						continue
				
				# Generate LDEV name with incremental number
				ldev_name = f"{base_ldev_name}_{name_start + i}"
				created_ldevs[ldev_id] = ldev_name
				
				# Update the command return view
				cmdreturn.view[ldev_id] = {
					'LDEV': ldev_id,
					'NAME': ldev_name,
					'PARITY_GRP': parity_grp_id,
					'CAPACITY': capacity_blocks,
					'EMULATION': emulation,
					'MP_BLADE_ID': mp_blade_id
				}
				
				# Update command return with result
				cmdreturn.stdout += result.stdout + "\n" if hasattr(result, 'stdout') else ""
				cmdreturn.stderr += result.stderr + "\n" if hasattr(result, 'stderr') else ""
				
				self.log.info(f"Successfully created physical volume {ldev_id}")
				
			except Exception as e:
				self.log.error(f"Error creating physical volume: {str(e)}")
				continue
		
		# Report what we were able to create
		self.log.info(f"Created {len(created_ldevs)} physical volumes out of {max_ldevs} maximum ({len(unavailable_ldevs)} unavailable)")
		
		# Step 2: Format the LDEVs if requested - FIXED IMPLEMENTATION
		if format_ldevs and created_ldevs:
			self.log.info(f"Starting quick format for {len(created_ldevs)} LDEVs")
			# import time
			
			time.sleep(3)
			# Start quick format for each LDEV - similar to how addmfpvols does it
			for ldev_id in created_ldevs:
				try:
					cmd = f"{self.path}raidcom initialize ldev -ldev_id {ldev_id} -operation qfmt -I{self.instance} -s {self.serial}"
					result = self.execute(cmd, **kwargs)
					
					if result.returncode == 0:
						self.log.info(f"Successfully started quick format for LDEV {ldev_id}")
					else:
						self.log.error(f"Failed to start quick format for LDEV {ldev_id}: {result.stderr}")
				except Exception as e:
					self.log.error(f"Error during quick format for LDEV {ldev_id}: {str(e)}")
			
			# Let the format operations complete in the background
			# The status will be checked when we get the LDEV details later
			# For now, just give some time for the format to progress
			self.log.info(f"Waiting for quick format operations to make progress...")
			time.sleep(3)  # Wait 30 seconds for formats to make progress
		else:
			self.log.info("Quick format not requested or no LDEVs to format")
		
		# Step 3: Now set the names for each LDEV with retry mechanism
		# Wait for 2 seconds to allow storage system to fully process the LDEV creations
		time.sleep(2)
		
		for ldev_id, ldev_name in created_ldevs.items():
			# Try up to 3 times to set the name
			max_retries = 3
			retry_delay = 2  # seconds
			success = False
			
			for attempt in range(max_retries):
				try:
					# Construct direct command for naming
					cmd = f"{self.path}raidcom modify ldev -ldev_id {ldev_id} -ldev_name {ldev_name} -I{self.instance} -s {self.serial}"
					result = self.execute(cmd=cmd, **kwargs)
					
					if result.returncode == 0:
						self.log.info(f"Successfully set name '{ldev_name}' for LDEV {ldev_id}")
						success = True
						break
					else:
						# If error is that LDEV is not installed, wait and retry
						if "LDEV is not installed" in result.stderr:
							self.log.warning(f"LDEV {ldev_id} not fully registered yet, waiting {retry_delay} seconds before retry {attempt+1}/{max_retries}")
							time.sleep(retry_delay)
							# Increase delay for next retry
							retry_delay += 2
						else:
							self.log.error(f"Failed to set name for LDEV {ldev_id}: {result.stderr}")
							break
				except Exception as e:
					self.log.error(f"Error setting name for LDEV {ldev_id}: {str(e)}")
					break
			
			if not success:
				self.log.error(f"Failed to set name for LDEV {ldev_id} after {max_retries} attempts")
		
		# Step 4: Wait longer to ensure all operations have completed, particularly the quick format
		# This approach mirrors the addmfpvols function which doesn't poll for format completion
		# but allows enough time for most formats to complete
		time.sleep(5)  # Increased wait time from 5 to 60 seconds
		
		# Store information about unavailable LDEVs
		cmdreturn.unavailable_ldevs = unavailable_ldevs
		
		# Populate data attribute with information about all created LDEVs
		for ldev_id, ldev_name in created_ldevs.items():
			# Create a basic LDEV data entry with the information we already have
			ldev_data = {
				'LDEV': ldev_id,
				'NAME': ldev_name,
				'PARITY_GRP': parity_grp_id,
				'CAPACITY': capacity_blocks,
				'EMULATION': emulation,
				'MP_BLADE_ID': mp_blade_id,
				'TYPE': 'OPEN-PVOL',
				'STATUS': 'NML'  # Default to NML, will be updated if we get actual status
			}
			
			# Try to get detailed information, but use our basic info if it fails
			if return_ldevs:
				try:
					# Get detailed information for this LDEV
					ldev_info = self.getldev(ldev_id=ldev_id, update_view=False, **kwargs)
					if hasattr(ldev_info, 'data') and ldev_info.data:
						# Use the detailed information
						ldev_data.update(ldev_info.data[0])
						
						# Check if the LDEV is still in BLK status
						if ldev_info.data[0].get('STATUS') == 'BLK':
							self.log.warning(f"LDEV {ldev_id} is still in BLK status after waiting - format may still be in progress")
							
					elif hasattr(ldev_info, 'view') and ldev_id in ldev_info.view:
						# Add the view data
						detailed_data = ldev_info.view[ldev_id]
						ldev_data.update(detailed_data)
						
						# Check if the LDEV is still in BLK status
						if detailed_data.get('STATUS') == 'BLK':
							self.log.warning(f"LDEV {ldev_id} is still in BLK status after waiting - format may still be in progress")
				except Exception as e:
					self.log.debug(f"Using basic info for LDEV {ldev_id} - detailed info unavailable: {str(e)}")
					pass
			
			# Add the LDEV data to our data list
			cmdreturn.data.append(ldev_data)
		
		# Return sorted view
		cmdreturn.view = dict(sorted(cmdreturn.view.items()))
		
		# Add a summary of what was created vs what was attempted
		cmdreturn.summary = {
			'max_ldevs': max_ldevs,
			'created_ldevs': len(created_ldevs),
			'unavailable_ldevs': len(unavailable_ldevs),
			'format_note': "Quick format operations continue asynchronously and may not be complete when this command returns"
		}
		
		return cmdreturn
		