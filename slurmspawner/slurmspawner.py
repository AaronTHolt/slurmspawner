"""SlurmSpawner implementation"""
import signal
import errno
import pwd
import os
import time
import pipes
from subprocess import Popen, call
import subprocess
from string import Template
from concurrent.futures import ThreadPoolExecutor

from tornado import gen

from traitlets import (
    Instance, Integer, Unicode
)

from jupyterhub.spawner import Spawner
from jupyterhub.spawner import set_user_setuid
from jupyterhub.utils import random_port


def run_command(cmd):
    popen = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out = popen.communicate()
    if out[1] is not None:
        return out[1] # exit error?
    else:
        out = out[0].decode().strip()
        return out


class SlurmSpawner(Spawner):
    """A Spawner that just uses Popen to start local processes."""

    _executor = None
    @property
    def executor(self):
        """single global executor"""
        cls = self.__class__
        if cls._executor is None:
            cls._executor = ThreadPoolExecutor(1)
        return cls._executor

    INTERRUPT_TIMEOUT = Integer(1200, config=True, \
        help="Seconds to wait for process to halt after SIGINT before proceeding to SIGTERM"
                               )
    TERM_TIMEOUT = Integer(1200, config=True, \
        help="Seconds to wait for process to halt after SIGTERM before proceeding to SIGKILL"
                          )
    KILL_TIMEOUT = Integer(1200, config=True, \
        help="Seconds to wait for process to halt after SIGKILL before giving up"
                          )

    ip = Unicode("0.0.0.0", config=True, \
        help="url of the server")

    slurm_job_id = Unicode() # will get populated after spawned

    pid = Integer(0)

    def make_preexec_fn(self, name):
        """make preexec fn"""
        return set_user_setuid(name)

    def load_state(self, state):
        """load slurm_job_id from state"""
        super(SlurmSpawner, self).load_state(state)
        self.slurm_job_id = state.get('slurm_job_id', '')

    def get_state(self):
        """add slurm_job_id to state"""
        state = super(SlurmSpawner, self).get_state()
        if self.slurm_job_id:
            state['slurm_job_id'] = self.slurm_job_id
        return state

    def clear_state(self):
        """clear slurm_job_id state"""
        super(SlurmSpawner, self).clear_state()
        self.slurm_job_id = ""

    def user_env(self, env):
        """get user environment"""
        env['USER'] = self.user.name
        env['HOME'] = pwd.getpwnam(self.user.name).pw_dir
        return env

    def _env_default(self):
        env = super()._env_default()
        return self.user_env(env)

    def _check_slurm_job_state(self):
        if self.slurm_job_id in (None, ""):
            # job has been cancelled or failed, so don't even try the squeue command. This is because
            # squeue will return RUNNING if you submit something like `squeue -h -j -o %T` and there's
            # at least 1 job running
            return ""
        # check sacct to see if the job is still running
        cmd = 'squeue -h -j ' + self.slurm_job_id + ' -o %T'
        out = run_command(cmd)
        self.log.info("Notebook server for user %s: Slurm jobid %s status: %s" % (self.user.name, self.slurm_job_id, out))
        return out

    def run_jupyterhub_singleuser(self, cmd, user):
        """ 
        Wrapper for calling run_jupyterhub_singleuser to be passed to ThreadPoolExecutor..
        """
        args = [cmd, user]
        return self.executor.submit(self._run_jupyterhub_singleuser, *args)

    def _run_jupyterhub_singleuser(self, cmd, user):
        """
        Submits a slurm sbatch script to start jupyterhub-singleuser
        """
        sbatch = Template('''#!/bin/bash
#SBATCH --partition=$queue
#SBATCH --time=$hours:00:00
#SBATCH -o /home/$user/jupyterhub_slurmspawner_%j.log
#SBATCH --job-name=spawner-jupyterhub
#SBATCH --workdir=/home/$user
#SBATCH --mem=$mem
#SBATCH --uid=$user
#SBATCH --get-user-env=L

which jupyterhub-singleuser
$export_cmd
$cmd
        ''')

        queue = "all"
        mem = '200'
        hours = '2'
        full_cmd = cmd.split(';')
        export_cmd = full_cmd[0] 
        cmd = full_cmd[1]
        sbatch = sbatch.substitute(dict(export_cmd=export_cmd, cmd=cmd, queue=queue, mem=mem, hours=hours, user=user))
        #serialsbatch+='cd %s' % "notebooks"
        print('Submitting *****{\n%s\n}*****' % sbatch)
        popen = subprocess.Popen('sbatch', shell = True, stdin = subprocess.PIPE, stdout = subprocess.PIPE)
        out = popen.communicate(sbatch.encode())[0].strip() #e.g. something like "Submitted batch job 209"
        return out

    def get_slurm_job_info(self, jobid):
        """returns ip address of node that is running the job"""
        cmd = 'squeue -h -j ' + jobid + ' -o %N'
        popen = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        node_name = popen.communicate()[0].strip().decode() # convett bytes object to string
        # now get the ip address of the node name
        cmd = 'host %s' % node_name
        popen = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        out = popen.communicate()[0].strip().decode()
        node_ip = out.split(' ')[-1] # the last portion of the output should be the ip address

        return node_ip

    @gen.coroutine
    def start(self):
        """Start the process"""
        self.user.server.port = random_port()
        cmd = []
        env = self.env.copy()

        cmd.extend(self.cmd)
        cmd.extend(self.get_args())

        self.log.debug("Env: %s", str(env))
        self.log.info("Spawning %s", ' '.join(cmd))
        for k in ["JPY_API_TOKEN"]:
            cmd.insert(0, 'export %s="%s";' % (k, env[k]))
        #self.pid, stdin, stdout, stderr = execute(self.channel, ' '.join(cmd))
        
        output = yield self.run_jupyterhub_singleuser(' '.join(cmd), self.user.name)
        output = output.decode() # convert bytes object to string
        self.log.debug("Stdout of trying to call run_jupyterhub_singleuser(): %s" % output)
        self.slurm_job_id = output.split(' ')[-1] # the job id should be the very last part of the string

        # make sure jobid is really a number
        try:
            int(self.slurm_job_id)
        except ValueError:
            self.log.info("sbatch returned this at the end of their string: %s" % self.slurm_job_id)

        #time.sleep(2)
        job_state = self._check_slurm_job_state()
        for i in range(15):
            self.log.info("job_state is %s" % job_state)
            if 'RUNNING' in job_state:
                break
            elif 'PENDING' in job_state:
                job_state = self._check_slurm_job_state()
                time.sleep(1)
            else:
                self.log.info("Job %s failed to start!" % self.slurm_job_id)
                return 1 # is this right? Or should I not return, or return a different thing?
        
        notebook_ip = self.get_slurm_job_info(self.slurm_job_id)

        self.user.server.ip = notebook_ip 
        self.log.info("Notebook server ip is %s" % self.user.server.ip)

    @gen.coroutine
    def poll(self):
        """Poll the process"""
        if self.slurm_job_id is not None:
            state = self._check_slurm_job_state()
            if "RUNNING" in state or "PENDING" in state:
                return None
            else:
                self.clear_state()
                return 1

        if not self.slurm_job_id:
            # no job id means it's not running
            self.clear_state()
            return 1

    @gen.coroutine
    def _signal(self, sig):
        """simple implementation of signal

        we can use it when we are using setuid (we are root)"""
        return True

    @gen.coroutine
    def stop(self, now=False):
        """stop the subprocess

        if `now`, skip waiting for clean shutdown
        """
        status = yield self.poll()
        if status is not None:
            # job is not running
            return

        cmd = 'scancel ' + self.slurm_job_id
        self.log.info("Cancelling slurm job %s for user %s" % (self.slurm_job_id, self.user.name))

        job_state = run_command(cmd)
        
        if job_state in ("CANCELLED", "COMPLETED", "FAILED", "COMPLETING"):
            return
        else:
            status = yield self.poll()
            if status is None:
                self.log.warn("Job %s never cancelled" % self.slurm_job_id)


if __name__ == "__main__":
    pass
