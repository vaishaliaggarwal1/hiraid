from ..execute_cci import execute
from ..cci_parser import Cci_parser

import logging
class EX_ATTHOR(Exception):
    pass

class Raidqry():
    acceptable_returns = [0]

    def __init__(self,log=logging,raise_err=True):
        self.log = log
        self.parser = Cci_parser(log=self.log)
        self.raise_err = raise_err
        
    def execute(self,cmd,**kwargs):
        cmdreturn = execute(cmd,log=self.log,raise_err=False)
        if self.raise_err:
            if cmdreturn.returncode == 251:
                raise EX_ATTHOR({'cmd':cmd, 'return':cmdreturn.returncode,'stdout':cmdreturn.stdout, 'stderr':cmdreturn.stderr })
            elif cmdreturn.returncode not in self.acceptable_returns:
                raise Exception({'cmd':cmd, 'return':cmdreturn.returncode,'stdout':cmdreturn.stdout, 'stderr':cmdreturn.stderr })
        
        if cmdreturn.returncode in self.acceptable_returns:
            self.parser.raidqry(cmdreturn,datafilter=kwargs.get('datafilter',{}))
        
        return cmdreturn
