# Work in progress

hiraid is a Python raidcom wrapper for communicating with Hitachi enterprise storage arrays. raidcom output is parsed to json and also stored beneath storageobject.views.

The primary purpose of this library is to underpin the Hitachi Vantara opensource ansible project: https://github.com/hitachi-vantara/hiraid-mainframe
## Install Latest

    pip3 install git+https://github.com/hitachi-vantara/hiraid-mainframe.git

    pip install hiraid-2.0.0-py3-none-any.whl

## Quick start

    from hiraid.raidcom import Raidcom
    storage_serial = 53511
    horcm_instance = 0
    storage = Raidcom(storage_serial,horcm_instance)
    ports = storage.getport()
    print(json.dumps(ports.view,indent=4))
    print(ports.data)
    print(json.dumps(ports.stats))

## Index your host groups, luns and associated ldevs

    storage.getpool(key='basic')
    ports = storage.getport().view.keys()
    hostgroups = storage.concurrent_gethostgrps(ports=ports)
    allhsds = [f"{port}-{gid}" for port in hostgroups.view for gid in hostgroups.view[port]['_GIDS']]
    storage.concurrent_getportlogins(ports=ports)
    storage.concurrent_gethbawwns(portgids=allhsds)
    storage.concurrent_getluns(portgids=allhsds)
    ldevlist = set([ self.raidcom.views['_ports'][port]['_GIDS'][gid]['_LUNS'][lun]['LDEV'] for port in self.raidcom.views['_ports'] for gid in self.raidcom.views['_ports'][port].get('_GIDS',{}) for lun in self.raidcom.views['_ports'][port]['_GIDS'][gid].get('LUNS',{}) ])
    storage.concurrent_getldevs(ldevlist)
    file = f"/var/tmp/{storage.serial}__{datetime.now().strftime('%d-%m-%Y%H.%M.%S')}.json"
    with open(file,'w') as w:
    w.write(json.dumps(storage.views,indent=4))

## raidqry

    rq = storage.raidqry()
    rq = storage.raidqry(datafilter={'Serial#':'350147'})
    rq = storage.raidqry(datafilter={'callable':lambda a : int(a['Cache(MB)']) > 50000})
    print(rq.data)
    print(rq.view)
    print(rq.cmd)
    print(rq.returncode)
    print(rq.stdout)
    print(rq.stderr)

## getldev

    l = storage.getldev(ldev_id=20000)
    l = storage.getldev(ldev_id=20000-21000,datafilter={'LDEV_NAMING':'HAVING_THIS_LABEL'})
    l = storage.getldev(ldev_id=20000-21000,datafilter={'callable':lambda a : float(a.get(Used_Block(GB)',0)) > 960000})

    for ldev in l.data:
    print(ldev['LDEV'])

## getport

    p = storage.getport()
    p = storage.getport(datafilter={'callable':lambda a : a['TYPE'] == 'FIBRE' and 'TAR' in a['ATTR']})

## gethostgrp

    h = storage.gethostgrp(port="cl1-a")
    h = storage.gethostgrp(port="cl1-a",datafilter={'HMD':''VMWARE_EX'})
    h = storage.gethostgrp(port="cl1-a",datafilter={'callable':lambda a : 'TEST' in a['GROUP_NAME']})

## gethostgrp_key_detail

    h = storage.gethostgrp_key_detail(port="cl1-a")
    h = storage.gethostgrp_key_detail(port="cl1-a",datafilter={'HMD':''VMWARE_EX'})
    h = storage.gethostgrp_key_detail(port="cl1-a",datafilter={'callable':lambda a : 'TEST' in a['GROUP_NAME']})

## getlun

    l = storage.getlun(port="cl1-a-1")
    l = storage.getlun(port="cl1-a-1",datafilter={'LDEV':['12001','12002']})
    l = storage.getlun(port="cl1-e-1",datafilter={'callable':lambda a : int(a['LUN']) > 10})
    l = storage.getlun(port="cl1-e-1",datafilter={'callable':lambda a : int(a['LDEV']) > 12000})

## getpool

    p = storage.getpool()

## getcommandstatus

### Disclaimer: 
All materials provided in this repository, including but not limited to Ansible Playbooks and Terraform
Configurations, are made available as a courtesy. These materials are intended solely as examples, which
may be utilized in whole or in part. Neither the contributors nor the users of this platform assert or are
granted any ownership rights over the content shared herein. It is the sole responsibility of the user to
evaluate the appropriateness and applicability of the materials for their specific use case.​

Use of the material is at the sole risk of the user and the material is provided “AS IS,” without warranty,
guarantees, or support of any kind, including, but not limited to, the implied warranties of merchantability,
fitness for a particular purpose, and non-infringement. Unless specified in an applicable license, access
to this material grants you no right or license, express or implied, statutorily or otherwise, under any
patent, trade secret, copyright, or any other intellectual property right of Hitachi Vantara LLC (“HITACHI”).
HITACHI reserves the right to change any material in this document, and any information and products on
which this material is based, at any time, without notice. HITACHI shall have no responsibility or liability to
any person or entity with respect to any damages, losses, or costs arising from the materials contained
herein.​
