#!/usr/bin/python3.6
# -----------------------------------------------------------------------------------------------------------------------------------
# Version v1.1.00
# -----------------------------------------------------------------------------------------------------------------------------------
#
# License Terms
# -------------
# Unless stated otherwise, Hitachi Vantara Limited and/or its group companies is/are the owner or the licensee
# of all intellectual property rights in this script. This work is protected by copyright laws and treaties around
# the world. This script is solely for use by Hitachi Vantara Limited and/or its group companies in the provision
# of services to you by Hitachi Vantara Limited and/or its group companies and, as a condition of your receiving
# such services, you expressly agree not to use, reproduce, duplicate, copy, sell, resell or exploit for any purposes,
# commercial or otherwise, this script or any portion of this script. All of Hitachi Vantara Limited and/or its
# group companies rights are reserved.
#
# -----------------------------------------------------------------------------------------------------------------------------------
# Changes:
#
# 14/01/2020    v1.1.00     Initial Release
#
# -----------------------------------------------------------------------------------------------------------------------------------

import traceback
import logging
class RaidcomException(Exception):

    def __init__(self, message, raidcom: object=None, log=logging ):
        '''
        message = output message
        raidcom = raidcom object
        log = logging object
        '''
        super(RaidcomException, self).__init__(message)
        self.log = log
        self.log.error(message)
        self.message = traceback.format_exc()
        self.messagein = message

        if raidcom.lock and raidcom.unlockOnException:
            raidcom.unlockresource()
        
        self.log.error("-- End on Error --")
    
    def __str__(self):
        if not self.message:
            return self.messagein
        return self.message
    
    def __exit__(self):
        print("EXIT CLASS")
        
