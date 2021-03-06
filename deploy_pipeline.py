#!/usr/bin/env python

"""Runs the various steps of our github-style deploy pipeline.

We use jenkins to implement a github-style deploy:
   https://docs.google.com/a/khanacademy.org/document/d/1s7qvACA4Uq4ON6F4PWJ_eyBz9EJeTk-DJ6SysRrJcTI/edit
   https://github.com/blog/1241-deploying-at-github

Unfortunately, Jenkins is not really meant for this kind of
pipelining.  We have at least 5 jenkins jobs:
   * kick-off job
   * build job
   * test job
   * [deploying to appengine is done by the kick-off job]
   * set-default job
   * finish job

We need build and test to be separate jenkins jobs because we want
them to run in parallel.  And we need kick-off/set-default/finish to
be separate jobs because there's a manual step involved between each
of these three tasks, and jenkins doesn't have great support for
manual steps.  (We simulate manual steps here by having each of these
jobs end with a hipchat message that includes a link to the next job
in the chain.)

All of these jobs must operate under a deploy lock, so we only do one
deploy at a time.  Also, these jobs all share global state, that is
specified when the kick-off job is started.

To make it easier to reason about the control flow, we put all the
work that these jobs do into one file, here.  Each job will run this
script with a different 'stage' value.  They will all verify they are
running under the lock.  They will all have access to the global state.

This script assumes that all the jobs in the pipeline run in the same
workspace, which will also hold the lockfile.
"""

import argparse
import cStringIO
import contextlib
import errno
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib
import urllib2

# This requires having secrets.py (or ka_secrets.py) on your PYTHONPATH!
sys.path.append(os.path.join(os.path.abspath(os.path.dirname(__file__)),
                             'alertlib'))
import alertlib

# We assume that webapp is a sibling to the jenkins-tools repo.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '..', 'webapp', 'tools'))
import appengine_tool_setup
appengine_tool_setup.fix_sys_path()

import deploy.deploy
import deploy.set_default
import ka_secrets             # for (optional) email->hipchat
from tools import manual_webapp_testing


# Used for testing.  Does not set-default, does not tag versions as bad.
_DRY_RUN = False

_WEBAPP_ROOT = os.path.dirname(os.path.abspath(ka_secrets.__file__))


def _alert(props, text, severity=logging.INFO, color=None, html=False,
           prefix_with_username=True):
    """Send the given text to hipchat and the logs."""
    if prefix_with_username:
        text = '%s %s' % (props['DEPLOYER_HIPCHAT_NAME'], text)
    (alertlib.Alert(text, severity=severity, html=html)
     .send_to_logs()
     .send_to_hipchat(room_name=props['HIPCHAT_ROOM'],
                      sender=props['HIPCHAT_SENDER'],
                      color=color, notify=True))


def _safe_urlopen(*args, **kwargs):
    """Does a urlopen with retries on error."""
    num_tries = 0
    while True:
        try:
            return urllib2.urlopen(*args, **kwargs)
        except Exception:
            num_tries += 1
            if num_tries == 3:
                raise
            else:
                logging.warning('url-fetch of %s %s failed, retrying...'
                                % (args, kwargs))


def _email_to_hipchat_name(email):
    """Given an email address, turn it into a @mention suitable for hipchat."""
    if email is None:
        return '<b>Unknown user:</b>'
    try:
        logging.info('Fetching email->hipchat mapping from hipchat')
        r = _safe_urlopen('https://api.hipchat.com/v1/users/list'
                          '?auth_token=%s' % ka_secrets.hipchat_deploy_token)
        user_data = json.load(r)
        email_to_mention_name = {user['email']: '@%s' % user['mention_name']
                                 for user in user_data['users']}
    except Exception, why:
        # If we don't have secrets, we get back a 401.  Ah well.
        logging.warning('Fetching email->hipchat mapping failed; will '
                        'guess. (%s: %s)' % (why.__class__.__name__, why))
        email_to_mention_name = {}
    # If we can't map email to hipchat name, just guess that their
    # hipchat name is @<email-name>.
    return email_to_mention_name.get(email, '@%s' % email.split('@')[0])


def _list_with_links(title_url_pairs):
    """Given a list of title/url, create html of the urls in a list form."""
    # I'll do five across.
    retval = []
    for i in xrange(0, len(title_url_pairs), 5):
        this_row = title_url_pairs[i:(i + 5)]
        retval.append(' ~ '.join('<a href="%s">%s</a>' % (url, title)
                                 for (title, url) in this_row))
    return '<br>\n'.join(retval)


def _run_command(cmd, failure_ok=False):
    """Return True if command succeeded, False else.  May raise on failure."""
    logging.info('Running command: %s' % cmd)
    if failure_ok:
        return subprocess.call(cmd, cwd=_WEBAPP_ROOT) == 0
    else:
        subprocess.check_call(cmd, cwd=_WEBAPP_ROOT)
        return True


def _pipe_command(cmd):
    logging.info('Running pipe-command: %s' % cmd)
    retval = subprocess.check_output(cmd, cwd=_WEBAPP_ROOT).rstrip()
    logging.info('>>> %s' % retval)
    return retval


@contextlib.contextmanager
def _password_on_stdin(pw_filename):
    """Run the context with stdin set to pw_filename's contents."""
    # Some code was originally written to read the password from
    # stdin.  Rather than refactoring so we can (also) pass in a
    # password directly, I just monkey-patch.
    with open(pw_filename) as f:
        password = f.read().strip()
    old_stdin = sys.stdin
    sys.stdin = cStringIO.StringIO(password)
    try:
        yield
    finally:
        sys.stdin = old_stdin


def _set_default_url(props, **extra_params):
    """Return a URL that points to the set-default job."""
    return ('%s/job/deploy-set-default/parambuild'
            '?TOKEN=%s&VERSION_NAME=%s&%s'
            % (props['JENKINS_URL'].rstrip('/'),
               props['TOKEN'],
               props['VERSION_NAME'],
               urllib.urlencode(extra_params)))


def _finish_url(props, **extra_params):
    """Return a URL that points to the deploy-finish job."""
    return ('%s/job/deploy-finish/parambuild?TOKEN=%s&%s'
            % (props['JENKINS_URL'].rstrip('/'),
               props['TOKEN'],
               urllib.urlencode(extra_params)))


def _gae_version(git_revision):
    # If git_revision is a branch, make sure it's available locally,
    # so dated_current_git_version can reference it.
    if _run_command(['git', 'ls-remote', '--exit-code',
                     '.', 'origin/%s' % git_revision],
                    failure_ok=True):
        _run_command(['git', 'fetch', 'origin',
                      '+refs/heads/%s:refs/remotes/origin/%s'
                      % (git_revision, git_revision)])
        git_revision = 'origin/%s' % git_revision
    return deploy.deploy.Git(_WEBAPP_ROOT).dated_current_git_version(
        git_revision)


def _current_gae_version():
    """The current default appengine version-name, according to appengine."""
    r = _safe_urlopen('http://www.khanacademy.org/api/internal/dev/version')
    version_dict = json.load(r)
    # The version-id is <major>.<minor>.  We just care about <major>.
    return version_dict['version_id'].split('.')[0]


def _create_properties(lockdir, deployer_email, git_revision,
                       auto_deploy, rollback_to,
                       jenkins_url, hipchat_room, hipchat_sender,
                       deploy_email, deploy_pw_file, token):
    """Return a dict of property-name to property value.

    Arguments:
        lockdir: the lock-directory, ideally an absolute path.  The
           existence of this directory indicates ownership of the lock.
        deployer_email: the (gmail) email of the person doing the
           deploy.  It's always the gmail email because that's how
           users authenticate with jenkins.
        git_revision: the branch-name (it can also just be a commit id)
           being deployed.
        auto_deploy: If 'true', don't ask whether to set the new version
           as the default, do so automatically.  Then does the
           set-default.py logs-monitoring.  If the monitoring
           indicates a potential problem, automatically roll back
           to the old deploy.
        rollback_to: the current appengine version before this deploy,
           that is, the appengine version-name we would roll back to
           if this deploy turned out to be problematic.
        jenkins_url: The url of the jenkins server.
        hipchat_room: The room to send all hipchat notifications to.
        hipchat_sender: The name to use as the sender of hipchat
           notifications.
        deploy_email: The AppEngine user to deploy as.
        deploy_pw_file: Filename of the file holding deploy_email's
           appengine password.
        token: a random string used to identify this deploy.  Future
           operations can supply a token and will fail unless their
           token value matches this one.
    """
    retval = {
        'LOCKDIR': lockdir,
        'DEPLOYER_EMAIL': deployer_email,
        'GIT_REVISION': git_revision,
        'AUTO_DEPLOY': str(auto_deploy).lower(),
        'ROLLBACK_TO': rollback_to,
        'JENKINS_URL': jenkins_url,
        'HIPCHAT_ROOM': hipchat_room,
        'HIPCHAT_SENDER': hipchat_sender,
        'DEPLOY_EMAIL': deploy_email,
        'DEPLOY_PW_FILE': deploy_pw_file,
        'TOKEN': token,
        }

    # Set some useful properties that we can derive from the above.
    retval['GIT_SHA1'] = retval['GIT_REVISION']
    retval['VERSION_NAME'] = _gae_version(retval['GIT_SHA1'])
    retval['DEPLOYER_USERNAME'] = retval['DEPLOYER_EMAIL'].split('@')[0]
    retval['DEPLOYER_HIPCHAT_NAME'] = (
        _email_to_hipchat_name(retval['DEPLOYER_EMAIL']))

    # These hold state about the deploy as it's going along.
    retval['LAST_ERROR'] = ''
    # A comma-separated list of choices taken from the 'action' argparse arg.
    retval['POSSIBLE_NEXT_STEPS'] = 'acquire-lock,finish-with-unlock,relock'

    # Note: GIT_SHA1 and VERSION_NAME will be updated after
    # merge_from_master(), which modifies the branch.

    logging.info('Setting deploy-properties: %s' % retval)
    return retval


def _read_properties(lockdir):
    """Read the properties from lockdir/deploy.prop into a dict."""
    retval = {}
    with open(os.path.join(lockdir, 'deploy.prop')) as f:
        for l in f.readlines():
            (k, v) = l.strip().split('=', 1)
            retval[k] = v

    # Do some sanity checking.
    assert retval['LOCKDIR'] == lockdir, (retval['LOCKDIR'], lockdir)

    logging.info('Read properties from %s: %s' % (lockdir, retval))
    return retval


def _write_properties(props):
    """Write the given properties dict into lockdir/deploy.prop."""
    logging.info('Wrote properties to %s: %s' % (props['LOCKDIR'], props))
    with open(os.path.join(props['LOCKDIR'], 'deploy.prop'), 'w') as f:
        for (k, v) in sorted(props.iteritems()):
            print >>f, '%s=%s' % (k, v)


def _update_properties(props, new_values):
    """Update props from the new_values dict, and write the result to disk.

    This routine also automatically updates dependent property values.
    For instance, whenever you change DEPLOYER_USERNAME, you also want
    to change DEPLOYER_HIPCHAT_NAME.  (You might ask: why store both
    when one can be derived from the other?  The answer is documentation:
    users can look at the properties file on disk and understand better
    what was going on.)
    """
    new_values = new_values.copy()
    if 'GIT_SHA1' in new_values:
        new_values.setdefault(
            'VERSION_NAME', _gae_version(new_values['GIT_SHA1']))

    if 'DEPLOYER_EMAIL' in new_values:
        new_values.setdefault(
            'DEPLOYER_USERNAME', new_values['DEPLOYER_EMAIL'].split('@')[0])

    if 'DEPLOYER_USERNAME' in new_values:
        new_values.setdefault(
            'DEPLOYER_HIPCHAT_NAME',
            _email_to_hipchat_name(new_values['DEPLOYER_USERNAME']))

    if 'LOCKDIR' in new_values:
        new_values.setdefault(
            'LOCK_ACQUIRE_TIME', int(time.time()))

    if 'POSSIBLE_NEXT_STEPS' in new_values:
        # finish-with-failure is always possible; it is called when
        # you manually cancel a jenkins job.  finish-with-rollback
        # too, which is mostly a synonym.  And finish-with-unlock and
        # relock, which are called manually when the script gets
        # messed up, and which we never want to block.
        next_steps = set(new_values['POSSIBLE_NEXT_STEPS'].split(','))
        next_steps.update(set(['finish-with-failure', 'finish-with-rollback',
                               'finish-with-unlock', 'relock']))
        new_values['POSSIBLE_NEXT_STEPS'] = ','.join(sorted(next_steps))

    props.update(new_values)
    _write_properties(props)


def acquire_deploy_lock(props, jenkins_build_url=None,
                        wait_sec=3600, notify_sec=600):
    """Acquire the deploy lock (a directory in the jenkins workspace).

    The deploy lock holds information about who is doing the deploy,
    as well as parameters they specify for the deploy.  Future deploy
    stages can use this.  The information is stored in a java-style
    properties file, so Jenkins rules can also use this as well:
    the properties file is <lockdir>/deploy.prop.

    Once the current lock-holder has held the lock for longer than
    wait_sec, we print an appropriate message to hipchat and exit.

    Arguments:
        props: a map of property-name to value, stored with the lock.
        jenkins_build_url: the 'build url' of the jenkins job trying
           to acquire the lock.  (This is $BUILD_URL inside jenkins,
           and looks something like
           http://jenkins.khanacademy.org/job/testjob/723/).
        wait_sec: how many seconds to busy-wait for the lock to free up.
        notify_sec: while waiting for the lock, how often to ping
           hipchat that we're still waiting.

    Raises:
        RuntimeError or OSError if we failed to acquire the lock.
    """
    # Assuming someone is holding the lock, who is it?
    lockdir = props['LOCKDIR']
    try:
        current_props = _read_properties(lockdir)
        # How long has the current lock-holder been holding this lock?
        waited_sec = int(time.time()) - int(current_props['LOCK_ACQUIRE_TIME'])
    except (IOError, OSError):
        current_props = {}
        waited_sec = 0

    done_first_alert = False
    while waited_sec < wait_sec:
        try:
            os.mkdir(lockdir)
        except OSError, why:
            if why.errno != errno.EEXIST:      # file exists
                raise
        else:                        # lockdir acquired!
            # We don't worry with timezones since the lock is always
            # local to a single machine, which has a consistent timezone.
            props['LOCK_ACQUIRE_TIME'] = int(time.time())
            logging.info("Lockdir %s acquired." % lockdir)
            msg = ""
            if done_first_alert:   # tell them they're no longer in line.
                msg += "Thank you for waiting! "
            msg += ("Starting deploy of branch %s.  I'll post to HipChat when "
                    "both a) tests are done and b) the deploy is finished."
                    % props['GIT_REVISION'])
            if jenkins_build_url and props['AUTO_DEPLOY'] != 'true':
                msg += ("  If you wish to cancel before then:\n"
                        "(failed) abort: %s/stop"
                        % jenkins_build_url.rstrip('/'))
            _alert(props, msg, color='green')
            return

        recover_msg = ("If this is a mistake and you are sure nobody else "
                       "is deploying, fix it by visiting "
                       "%s/job/deploy-finish/build, setting STATUS=unlock "
                       "and clicking 'Build'."
                       % (props['JENKINS_URL'].rstrip('/')))
        if not done_first_alert:
            _alert(props,
                   "You're next in line to deploy! (branch %s.) "
                   "Currently deploying (%.0f minutes in so far): "
                   "%s (branch %s). %s"
                   % (props['GIT_REVISION'],
                      waited_sec / 60.0,
                      current_props.get('DEPLOYER_USERNAME', 'Unknown User'),
                      current_props.get('GIT_REVISION', 'unknown'),
                      recover_msg),
                   color='yellow')
            done_first_alert = True
        elif waited_sec % notify_sec == 0:
            _alert(props,
                   "You're still next in line to deploy, after %s (branch %s)."
                   " (Waited %.0f minutes so far). %s"
                   % (current_props.get('DEPLOYER_USERNAME', 'Unknown User'),
                      current_props.get('GIT_REVISION', 'unknown'),
                      waited_sec / 60.0,
                      recover_msg),
                   color='yellow')

        time.sleep(10)     # how often to busy-wait
        waited_sec += 10

    # Figure out where in the pipeline the previous job is, and
    # suggest a course of action based on that.
    next_steps = current_props['POSSIBLE_NEXT_STEPS'].split(',')
    if ('merge-from-master' in next_steps or 'manual-test' in next_steps or
            'set-default' in next_steps):
        # They haven't set the default yet, so we can just fail.
        msg = ("(failed) cancel their deploy: %s"
               % _finish_url(current_props, STATUS='failure', WHY='aborted'))
    elif 'finish-with-success' in next_steps:
        msg = ("(successful) finish their deploy with success: %s\n"
               "(failed) abort their deploy and roll back: %s"
               % (_finish_url(current_props, STATUS='success'),
                  _finish_url(current_props, STATUS='rollback', WHY='aborted',
                              ROLLBACK_TO=current_props['ROLLBACK_TO'])))
    else:
        msg = ("(continue) release the lock: %s"
               % _finish_url(current_props, STATUS='unlock'))

    _alert(props,
           "%s has been deploying for over %s minutes. "
           "Perhaps it's a stray lock?  If you are confident that "
           "no deploy is currently running (check the dashboard at %s), "
           "you can:\n"
           "%s\n"
           "Once you done this, you will need to re-start your own deploy."
           % (current_props['DEPLOYER_USERNAME'],
              waited_sec / 60,
              current_props['JENKINS_URL'],
              msg),
           severity=logging.ERROR)
    raise RuntimeError('Timed out waiting on the current lock-holder.')


def _move_lockdir(props, old_lockdir, new_lockdir):
    """Re-acquire the lock in new_lockdir with the values in old_lockdir.

    new_lockdir must not exist (meaning that nobody else is holding the
    lock there).  If new_lockdir does exist, raise an OSError.

    Raises:
        OSError if we failed to move the lockdir.
    """
    if os.path.exists(new_lockdir):
        raise OSError('Lock already held in "%s"' % new_lockdir)

    logging.info('Renaming %s -> %s' % (old_lockdir, new_lockdir))
    os.rename(old_lockdir, new_lockdir)

    # Update the LOCKDIR property to point to the new location.
    _update_properties(props, {'LOCKDIR': os.path.abspath(new_lockdir)})


def release_deploy_lock(props, backup_lockfile=True):
    """Raise RuntimeError if the release failed."""
    # We move the lockdir to a 'backup' lockdir in case it turns out
    # we want to re-acquire this lock with the same parameters.
    # (This might happen if we released the lockdir in error.)
    lockdir = props['LOCKDIR']
    old_lockdir = lockdir + '.last'

    try:
        shutil.rmtree(old_lockdir)
    except OSError:        # probably 'dir does not exist'
        pass

    try:
        if backup_lockfile:
            _move_lockdir(props, lockdir, old_lockdir)
        else:
            shutil.rmtree(lockdir)
    except (IOError, OSError), why:
        _alert(props,
               "Could not release the deploy-lock (%s); it's not being held? "
               "(%s)" % (lockdir, why),
               severity=logging.ERROR)
        raise RuntimeError('Could not release the deploy-lock (%s)' % why)

    logging.info('Released the deploy lock: %s' % lockdir)


def merge_from_master(props):
    """Merge master into the current branch if necessary.

    Given an argument that contains either the name of a branch or a
    different kind of commit-ish (sha1, tag, etc) in GIT_REVISION,
    does two things:

    1) Ensures that HEAD matches that argument -- that is, that you're
       checked out where you expect to be -- and then does a
       git checkout <branch> so we are no longer in a detached-head
       state.

    2) Check if the input sha1 is a superset of master (that is,
       everything in master is part of this sha1's history too).
       If not:
    2a) If the argument is a branch-name, merge master into the branch.
    2b) If the argument is another commit-ish, fail.

    Raises:
       ValueError, RuntimeError, or subprocess.CalledProcessError if
       the merge from master failed for any reason.  This means we
       should abort the build and release the lock.
    """
    git_revision = props['GIT_REVISION']
    if git_revision == 'master':
        raise ValueError("You must deploy from a branch, you can't deploy "
                         "from master")

    # Make sure our local 'master' matches the remote.
    _run_command(['git', 'fetch', 'origin',
                  '+refs/heads/master:refs/remotes/origin/master'])
    _run_command(['git', 'checkout', 'master'])
    _run_command(['git', 'reset', '--hard', 'origin/master'])

    # Set our local branch to be the same as the origin branch.  This
    # is needed in cases when a previous deploy set the local (jenkins)
    # branch to commit X, but subsequent commits have moved the remote
    # (github) version of the branch to commit Y.  This also moves us
    # from a (potentially) detached-head state to a head-at-branch state.
    # Finally, it makes sure the ref exists locally, so we can do
    # 'git rev-parse branch' rather than 'git rev-parse origin/branch'
    # (though only if we're given a branch rather than a commit).
    if _run_command(['git', 'ls-remote', '--exit-code',
                     '.', 'origin/%s' % git_revision],
                    failure_ok=True):
        _run_command(['git', 'fetch', 'origin',
                      '+refs/heads/%s:refs/remotes/origin/%s'
                      % (git_revision, git_revision)])
        # The '--' is needed if git_revision is both a branch and
        # directory, e.g. 'sat'.  '--' says 'treat it as a branch'.
        _run_command(['git', 'checkout', git_revision, '--'])
        _run_command(['git', 'reset', '--hard', 'origin/%s' % git_revision])
    else:
        _run_command(['git', 'checkout', git_revision, '--'])

    head_commit = _pipe_command(['git', 'rev-parse', 'HEAD'])
    master_commit = _pipe_command(['git', 'rev-parse', 'master'])

    # Sanity check: HEAD should be at the revision we want to deploy from.
    if head_commit != _pipe_command(['git', 'rev-parse', git_revision]):
        raise RuntimeError('HEAD unexpectedly at %s, not %s'
                           % (head_commit, git_revision))

    # If the current commit is a super-set of master, we're done, yay!
    base = _pipe_command(['git', 'merge-base', git_revision, master_commit])
    if base == master_commit:
        logging.info('%s is a superset of master, no need to merge'
                     % git_revision)
        return

    # Now we need to merge master into our branch.  First, make sure
    # we *are* a branch.  git show-ref returns line like 'd41eba92 refs/...'
    all_branches = _pipe_command(['git', 'show-ref']).splitlines()
    all_branch_names = [l.split()[1] for l in all_branches]
    if ('refs/remotes/origin/%s' % git_revision) not in all_branch_names:
        raise ValueError('%s is not a branch name on the remote, like these:'
                         '\n  %s' % ('\n  '.join(sorted(all_branch_names))))

    # The merge exits with rc > 0 if there were conflicts
    logging.info("Merging master into %s" % git_revision)
    try:
        _run_command(['git', 'merge', 'master'])
    except subprocess.CalledProcessError:
        _run_command(['git', 'merge', '--abort'])
        raise RuntimeError('Merge conflict: must merge master into %s '
                           'manually.' % git_revision)

    # There's a race condition if someone commits to this branch while
    # this script is running, so check for that.
    try:
        _run_command(['git', 'push', 'origin', git_revision])
    except subprocess.CalledProcessError:
        _run_command(['git', 'reset', '--hard', head_commit])
        raise RuntimeError("Someone committed to %s while we've been "
                           "deploying!" % git_revision)

    logging.info("Done merging master into %s" % git_revision)


def _tag_release(props):
    """Tag the github commit that was deployed with the deploy-name."""
    tag_name = 'gae-%s' % props['VERSION_NAME']
    # Don't try to re-create the tag if it already exists.
    if not _pipe_command(['git', 'tag', '-l', tag_name]):
        _run_command(
            ['git', 'tag',
             '-m',
             'Deployed to appengine from branch %s' % props['GIT_REVISION'],
             tag_name,
             props['GIT_SHA1']])


def _tag_as_bad_version(props):
    """Tag the currently deployed github commit as having problems."""
    tag_name = 'gae-%s-bad' % props['VERSION_NAME']
    # Don't try to re-create the tag if it already exists.
    if not _pipe_command(['git', 'tag', '-l', tag_name]):
        _run_command(
            ['git', 'tag',
             '-m', 'Bad version (%s): rolled back' % props['VERSION_NAME'],
             tag_name,
             props['GIT_SHA1']])


def merge_to_master(props):
    """Merge from the current branch into master.

    This is called after a successful deploy, right before releasing
    the lock.  It maintains the invariant that master holds the code
    for the latest successful deploy.

    Given an argument that holds the deployed-sha1 in GIT_SHA1, merges
    that into master and pushes.  In a perfect world -- that is, one
    in which people don't commit to master manually, it only happens
    via this function -- this will be a fast-forward merge, since we
    already required that our branch be a superset of master in
    merge_from_master().

    Raises:
       RuntimeError or subprocess.CalledProcessError if the merge
       failed, meaning we should abort the build and release the lock.
    """
    if _DRY_RUN:
        return

    branch_name = '%s (%s)' % (props['GIT_SHA1'], props['GIT_REVISION'])

    # Set our local version of master to be the same as the origin
    # master.  This is needed in cases when a previous deploy set the
    # local (jenkins) master to commit X, but subsequent commits have
    # moved the remote (github) version of master to commit Y.  It
    # also makes sure the ref exists locally, so we can do the merge.
    _run_command(['git', 'fetch', 'origin',
                  '+refs/heads/master:refs/remotes/origin/master'])
    _run_command(['git', 'checkout', 'master'])
    _run_command(['git', 'reset', '--hard', 'origin/master'])
    head_commit = _pipe_command(['git', 'rev-parse', 'HEAD'])

    # The merge exits with rc > 0 if there were conflicts
    logging.info("Merging %s into master" % branch_name)
    try:
        _run_command(['git', 'merge', props['GIT_SHA1']])
    except subprocess.CalledProcessError:
        _run_command(['git', 'merge', '--abort'], failure_ok=True)
        raise

    # There's a race condition if someone commits to master while this
    # script is running, so check for that.
    try:
        _run_command(['git', 'push', '--tags', 'origin', 'master'])
    except subprocess.CalledProcessError:
        _run_command(['git', 'reset', '--hard', head_commit],
                     failure_ok=True)
        raise

    logging.info("Done merging %s into master" % branch_name)


def _rollback_deploy(props):
    """Roll back to ROLLBACK_TO and tag the current deploy as bad.

    Returns True if rollback succeeded -- even if we failed to tag the
    version as bad after rolling back from it -- False else.
    """
    current_gae_version = _current_gae_version()
    if current_gae_version != props['VERSION_NAME']:
        logging.info("Skipping rollback: looks like our deploy never "
                     "succeeded. (Us: %s, current: %s, rollback-to: %s)"
                     % (props['VERSION_NAME'], current_gae_version,
                        props['ROLLBACK_TO']))
        return True

    _alert(props,
           "Automatically rolling the default back to %s "
           "and tagging %s as bad (in git)"
           % (props['ROLLBACK_TO'], props['VERSION_NAME']))
    try:
        logging.info('Tagging %s as a bad version' % props['VERSION_NAME'])
        _tag_as_bad_version(props)
        _run_command(['git', 'push', '--tags'])

        logging.info('Calling set_default to %s' % props['ROLLBACK_TO'])
        with _password_on_stdin(props['DEPLOY_PW_FILE']):
            deploy.set_default.main(props['ROLLBACK_TO'],
                                    email=props['DEPLOY_EMAIL'],
                                    passin=True,
                                    num_instances_to_prime=None,
                                    monitor_minutes=0,
                                    hipchat_room=props['HIPCHAT_ROOM'],
                                    dry_run=_DRY_RUN)

        # If the version we rolled back *to* is marked bad, warn about that.
        if _pipe_command(['git', 'tag', '-l',
                          '%s-bad' % props['ROLLBACK_TO']]):
            _alert(props,
                   "(poo) WARNING: Rolled back to %s, but that version "
                   "has itself been marked as bad.  You may need to manually "
                   "run set_default.py to roll back to a safe version.  (Run "
                   "'git tag' to see all versions, good and bad.)"
                   % props['ROLLBACK_TO'])
    except Exception:
        logging.exception('Auto-rollback failed')
        _alert(props,
               "(sadpanda) (sadpanda) Auto-rollback failed! "
               "Roll back to %s manually by running: deploy/set_default.py %s"
               % (props['ROLLBACK_TO'], props['ROLLBACK_TO']),
               severity=logging.CRITICAL)
        return False

    return True


def manual_test(props):
    """Send a message to hipchat saying to do pre-set-default manual tests."""
    hostname = '%s-dot-khan-academy.appspot.com' % props['VERSION_NAME']
    _alert(props,
           "https://%s/ (branch %s) is uploaded to appengine! "
           "Do some manual testing on it, then either:\n"
           "(successful) set it as default: type 'sun, set default' or "
           "visit %s\n"
           "(failed) abort the deploy: type 'sun, abort' or visit %s"
           % (hostname, props['GIT_REVISION'],
              _set_default_url(props, AUTO_DEPLOY=props['AUTO_DEPLOY']),
              _finish_url(props, STATUS='failure', WHY='aborted')),
           color='green')
    time.sleep(1)   # to help the two hipchat alerts be ordered properly

    # Suggest some urls to do for manual testing, as both links and a
    # commandline tool.
    _alert(props,
           ("Here are some pages to manually test:<br>%s<br>"
            "Or open them all at once (cut-and-paste): "
            "<b>tools/manual_webapp_testing.py %s</b><br>"
            "Also run end-to-end testing (cut-and-paste): "
            "<b>tools/end_to_end_webapp_testing.py --version %s</b>"
            % (manual_webapp_testing.list_with_links(props['VERSION_NAME']),
               props['VERSION_NAME'], props['VERSION_NAME'])),
           html=True, prefix_with_username=False)


def set_default(props, monitoring_time=10, jenkins_build_url=None):
    """Call set_default.py to make a specified deployed version live.

    If the user asked for monitoring, also do the monitoring, potentially
    rolling back if there's a problem.

    Raises:
        RuntimeError or deploy.set_default.MonitoringError if we
        encountered an error that should cause the build to abort and
        jenkins to release the build lock.  We do not raise an
        exception if the build should continue, either because we
        emitted a hipchat message telling the user to click on a link
        to take us to the next step, or because set-default failed and
        was successfully auto-rolled back.
    """
    logging.info("Changing default from %s to %s"
                 % (props['ROLLBACK_TO'], props['VERSION_NAME']))
    # I do the deploy steps one at a time so I can intersperse some
    # hipchat mesasges.
    did_priming = False
    try:
        pre_monitoring_data = deploy.set_default.get_predeploy_monitoring_data(
            monitoring_time)

        logging.info("Priming 100 instances")
        deploy.set_default.prime(version=props['VERSION_NAME'],
                                 num_instances_to_prime=100,
                                 dry_run=_DRY_RUN)
        did_priming = True

        logging.info("Setting default")
        with _password_on_stdin(props['DEPLOY_PW_FILE']):
            deploy.set_default.set_default(version=props['VERSION_NAME'],
                                           email=props['DEPLOY_EMAIL'],
                                           passin=True,
                                           dry_run=_DRY_RUN)

        if (monitoring_time and jenkins_build_url and
                props['AUTO_DEPLOY'] != 'true'):
            _alert(props,
                   "I've deployed to %s, and will be monitoring "
                   "logs for %s minutes.  After that, I'll post "
                   "next steps to HipChat.  If you detect a problem in "
                   "the meantime you can cancel the deploy (note: this "
                   "link will only work for the next %s minutes):\n"
                   "(failed) abort and rollback: %s/stop"
                   % (props['VERSION_NAME'], monitoring_time, monitoring_time,
                      jenkins_build_url.rstrip('/')))
            time.sleep(1)  # to help the two hipchat alerts be ordered properly
            _alert(props,
                   ("While that's going on, manual-test on the live site!<br>"
                    "%s<br>\n"
                    "Or open them all at once (cut-and-paste): "
                    "<b>tools/manual_webapp_testing.py %s</b><br>"
                    "Also run end-to-end testing (cut-and-paste): "
                    "<b>tools/end_to_end_webapp_testing.py --version %s</b>"
                    % (manual_webapp_testing.list_with_links('default'),
                       'default', 'default')),
                   html=True, prefix_with_username=False)

        deploy.set_default.monitor(props['VERSION_NAME'], monitoring_time,
                                   pre_monitoring_data,
                                   hipchat_room=props['HIPCHAT_ROOM'])

    except deploy.set_default.MonitoringError, why:
        # Wait a little to make sure this hipchat message comes after
        # the "I've deployed to ..." message we emitted above above.
        time.sleep(10)
        if props['AUTO_DEPLOY'] == 'true':
            _alert(props,
                   "(sadpanda) %s." % why,
                   severity=logging.WARNING)
            # By re-raising, we trigger the deploy-set-default jenkins
            # job to clean up by rolling back.  auto-rollback for free!
            raise
        else:
            _alert(props,
                   "(sadpanda) %s. "
                   "Make sure everything is ok, then:\n"
                   "(successful) finish up: type 'sun, finish up' "
                   "or visit %s\n"
                   "(failed) abort and roll back: type 'sun, abort' "
                   "or visit %s"
                   % (why,
                      _finish_url(props, STATUS='success'),
                      _finish_url(props, STATUS='rollback', WHY='aborted',
                                  ROLLBACK_TO=props['ROLLBACK_TO'])
                      ),
                   severity=logging.WARNING)
    except Exception:
        logging.exception('set-default failed')
        if props['AUTO_DEPLOY'] == 'true':
            _alert(props, "(sadpanda) (sadpanda) set-default failed!",
                   severity=logging.ERROR)
            raise

        if did_priming:
            priming_flag = '--no-priming '
        else:
            priming_flag = ''

        _alert(props,
               "(sadpanda) (sadpanda) set-default failed!  Either:\n"
               "(continue) Set the default to %s manually (by running "
               "deploy/set_default.py %s%s), then release the deploy lock "
               "via %s\n"
               "(failed) abort and roll back %s"
               % (props['VERSION_NAME'], priming_flag, props['VERSION_NAME'],
                  _finish_url(props, STATUS='success'),
                  _finish_url(props, STATUS='rollback', WHY='aborted',
                              ROLLBACK_TO=props['ROLLBACK_TO'])),
               severity=logging.CRITICAL)
    else:
        # No need for a hipchat message if the next step is automatic.
        if props['AUTO_DEPLOY'] != 'true':
            _alert(props,
                   "Monitoring passed for the new default (%s)! "
                   "But you should double-check everything "
                   "is ok at https://www.khanacademy.org. "
                   "Then:\n"
                   "(successful) finish up: type 'sun, finish up' "
                   "or visit %s\n"
                   "(failed) abort and roll back: type 'sun, abort' "
                   "or visit %s"
                   % (props['VERSION_NAME'],
                      _finish_url(props, STATUS='success'),
                      _finish_url(props, STATUS='rollback', WHY='aborted',
                                  ROLLBACK_TO=props['ROLLBACK_TO'])),
                   color='green')


def finish_with_unlock(props, caller):
    """Manually release the deploy lock.  Caller is the 'manual' person.

    This is called when something is messed up and the lock is being
    held even though no deploy is going on.

    Raises RuntimeError if we failed to release the lock.
    """
    if caller == props['DEPLOYER_HIPCHAT_NAME']:
        # You are releasing your own lock
        _alert(props, "has manually released the deploy lock.")
    else:
        _alert(props,
               ": %s has manually released the deploy lock." % caller)
    release_deploy_lock(props)


def finish_with_success(props):
    """Release the deploy lock because the deploy succeeded.

    We also merge the deployed commit to master, to maintain the
    invariant that 'master' holds the last successful deploy.

    Raises RuntimeError or subprocess.CalledProcessError if we failed
    to release the lock or if we failed to finish the deploy
    (e.g. failed to merge back into master.)
    """
    # We don't want to tag the release if the user ran with DEPLOY=no.
    # We tell by checking if the current gae version is VERSION_NAME.
    if _current_gae_version() == props['VERSION_NAME']:
        _tag_release(props)
    try:
        merge_to_master(props)
    except Exception:
        _alert(props,
               "(sadpanda) Deploy of %s (branch %s) succeeded, "
               "but we did not successfully merge %s into master. "
               "Merge and push manually, then release the lock: %s"
               % (props['VERSION_NAME'], props['GIT_REVISION'],
                  props['GIT_REVISION'], _finish_url(props, STATUS='unlock')),
               severity=logging.ERROR)
        raise

    _alert(props,
           "(gangnamstyle) Deploy of %s (branch %s) succeeded! "
           "Time for a happy dance!"
           % (props['VERSION_NAME'], props['GIT_REVISION']),
           color='green')
    release_deploy_lock(props, backup_lockfile=False)


def finish_with_failure(props):
    """Release the deploy lock after a failed deploy, or raise if we can't."""
    if props['LAST_ERROR']:
        why = ": %s" % props['LAST_ERROR']
    else:
        why = ". I'm sorry."
    _alert(props,
           "(pokerface) Deploy of %s (branch %s) failed%s"
           % (props['VERSION_NAME'], props['GIT_REVISION'], why),
           severity=logging.ERROR)
    release_deploy_lock(props)


def finish_with_rollback(props):
    """Does a rollback and releases the lock if it succeeds."""
    if props['LAST_ERROR']:
        _alert(props,
               "Rolling back %s due to problems with the deploy: %s"
               % (props['VERSION_NAME'], props['LAST_ERROR']),
               severity=logging.ERROR)
    if not _rollback_deploy(props):
        _alert(props,
               "Once you have manually rolled back, release the deploy "
               "lock: %s" % _finish_url(props, STATUS='unlock'),
               severity=logging.ERROR)
        raise RuntimeError('Failed to roll back to the previous deploy.')
    finish_with_failure(props)


def relock(props):
    """Re-acquires the lockdir from a backup lockdir directory.

    You call relock with --lockdir=<somedir>.last.  It then renames
    <somedir>.last to <somedir>, thus re-acquiring the lock in
    <somedir>.

    Raises OSError or ValueError if we can't relock, probably because
    someone else has acquired the lock themselves before we could
    re-acquire it.
    """
    old_lockdir = props['LOCKDIR']
    new_lockdir = old_lockdir[:-len('.last')]

    if not old_lockdir.endswith('.last'):
        logging.error('Unexpected value for --lockdir: "%s" does not end '
                      'with ".last"' % props['LOCKDIR'])
        raise ValueError('lockdir "%s" does not end with ".last"'
                         % props['LOCKDIR'])

    if os.path.exists(new_lockdir):
        logging.error("Cannot relock %s -- someone else has already "
                      "acquired the lock since you released it." % new_lockdir)
        raise OSError('%s already exists' % new_lockdir)

    _move_lockdir(props, old_lockdir, new_lockdir)


def main(action, lockdir, acquire_lock_args=(),
         token=None, monitoring_time=None, jenkins_build_url=None,
         caller_email=None):
    """action is one of:
    * acquire-lock: acquire the deploy lock.
    * merge-from-master: merge master into current branch if necessary.
    * manual-test: send a hipchat message saying to do pre-set-default testing.
    * set-default: set this version to GAE default after it's been uploaded.
    * finish-with-unlock: manually release the deploy lock.
    * finish-with-success: ditto, but because the deploy succeeded.
    * finish-with-failure: ditto, but because the deploy failed (pre-set-dflt)
    * finish-with-rollback: ditto, because the deploy failed (post-set-default)
    * relock: re-acquire the lock from lockdir.last, if possible.

    If action is acquire-lock, then acquire_lock_args should be
    specified as a list of the arguments to _create_properties().

    monitoring_time is ignored except by set_default.

    caller_email is ignored except by finish-with-unlock (and also
    if we can't acquire the lock).

    If token is non-empty, then whenever we do an operation we first
    check that the specified token matches the TOKEN value from the
    lockfile.  If not, then we know that this operation is not
    associated with the current lock, and we fail it.

    The commands raise an exception if we should stop the pipeline
    there, due to failure.  In those cases, we return False, which
    will cause the calling Jenkins job to abort and try to release the
    lock.  (Exception: the deploy-finish Jenkins job does not try to
    release the lock when we return False, though it does abort,
    because the usual reason a finish_* step fails is because it tried
    to release the lock and couldn't, so why try again?)  On the other
    hand, if the command does not raise an exception, we return True,
    which will cause the Jenkins job to say that this step succeeded.
    """
    if action == 'acquire-lock':
        props = _create_properties(*acquire_lock_args)
    else:
        try:
            props = _read_properties(lockdir)
        except IOError, why:
            if action == 'relock':
                logging.exception('There is no backup lock at %s to '
                                  'recover from, sorry.' % lockdir)
            else:
                # We can't load the real props, so do the best we can.
                fake_props = {'DEPLOYER_HIPCHAT_NAME':
                                 _email_to_hipchat_name(caller_email),
                              'HIPCHAT_ROOM': '1s/0s: deploys',
                              'HIPCHAT_SENDER': 'Mr Gorilla',
                              'JENKINS_URL': 'http://jenkins.khanacademy.org/',
                              'TOKEN': '',
                              }
                _alert(fake_props,
                       '(sadpanda) Trying to run without the lock. '
                       'If you think you *should* have the lock, '
                       'try to re-acquire it: %s. Then run your command again.'
                       % _finish_url(fake_props, STATUS='relock'),
                       severity=logging.ERROR)
            return False

    # If the passed-in token doesn't match the token in props, then
    # we are not the owners of this lock, so fail.  This is an
    # optional check.
    if token and props.get('TOKEN') and token != props['TOKEN']:
        _alert(props,
               'You do not own the deploy lock (its token is %s, '
               'yours is %s); aborting' % (props['TOKEN'], token),
               severity=logging.ERROR,
               # The username in props probably isn't right -- if we
               # don't match prop's token, why would we match its
               # username? -- so don't prepend it.
               # TODO(csilvers): pass in the *right* username instead.
               prefix_with_username=False)
        # We definitely don't want jenkins to release the lock, since
        # we don't own it.  So we have to return True.
        return True

    # If the step we're taking doesn't match a legal next-step in the
    # pipeline, fail.
    if (action not in props['POSSIBLE_NEXT_STEPS'].split(',') and
           '<all>' not in props['POSSIBLE_NEXT_STEPS'].split(',')):
        _alert(props,
               'Expecting you to run %s, but you are running %s. '
               'Perhaps you double-clicked on a link?  Ignoring.'
               % (" or ".join(props['POSSIBLE_NEXT_STEPS'].split(",")),
                  action),
               severity=logging.ERROR)
        # We just ignore this action, so we don't release the lock.
        return True

    try:
        if action == 'acquire-lock':
            acquire_deploy_lock(props, jenkins_build_url)
            _write_properties(props)
            _update_properties(props,
                               {'POSSIBLE_NEXT_STEPS': 'merge-from-master'})

        elif action == 'merge-from-master':
            merge_from_master(props)
            # Now we need to update the props file to indicate the new
            # GIT_SHA1 after merging.  (This also updates VERSION_NAME.)
            sha1 = _pipe_command(['git', 'rev-parse', props['GIT_REVISION']])
            # We can go straight to set-default if the user ran with
            # AUTO_DEPLOY, and straight to finish if they ran with DEPLOY=no.
            if props['AUTO_DEPLOY'] == 'true':
                next_steps = 'set-default'
            else:
                next_steps = 'manual-test'
            # We don't know if the user ran with DEPLOY=no, so always allow it.
            next_steps += ',finish-with-success'
            _update_properties(props,
                               {'GIT_SHA1': sha1,
                                'POSSIBLE_NEXT_STEPS': next_steps})

        elif action == 'manual-test':
            manual_test(props)
            _update_properties(props,
                               {'POSSIBLE_NEXT_STEPS': 'set-default'})

        elif action == 'set-default':
            set_default(props, monitoring_time=monitoring_time,
                        jenkins_build_url=jenkins_build_url)
            # If set_default didn't raise an exception, all is happy.
            if props['AUTO_DEPLOY'] == 'true':
                finish_with_success(props)
            else:
                _update_properties(props,
                                   {'POSSIBLE_NEXT_STEPS':
                                    'finish-with-success'})

        elif action == 'finish-with-unlock':
            finish_with_unlock(props, _email_to_hipchat_name(caller_email))

        elif action == 'finish-with-success':
            finish_with_success(props)

        elif action == 'finish-with-failure':
            finish_with_failure(props)

        elif action == 'finish-with-rollback':
            finish_with_rollback(props)

        elif action == 'relock':
            relock(props)
            # You relock when something went wrong, so any step could
            # legitimately go next.
            _update_properties(props,
                               {'POSSIBLE_NEXT_STEPS': '<all>'})

        else:
            raise RuntimeError("Unknown action '%s'" % action)

        if os.path.exists(os.path.join(props['LOCKDIR'], 'deploy.prop')):
            _update_properties(props, {'LAST_ERROR': ''})
        return True
    except Exception, why:
        logging.exception(action)
        if action != 'acquire-lock':
            # Don't write the properties file if we failed in trying
            # to acquire the lock! -- writing the properties file
            # would then acquire the lock for us by accident.
            _update_properties(props, {'LAST_ERROR': str(why)})
        return False


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action',
                        choices=('acquire-lock',
                                 'merge-from-master',
                                 'manual-test',
                                 'set-default',
                                 'finish-with-unlock',
                                 'finish-with-success',
                                 'finish-with-failure',
                                 'finish-with-rollback',
                                 'relock',
                                 ),
                        help='Action to perform')
    parser.add_argument('--lockdir',
                        default='tmp/deploy.lockdir',
                        help=("The lock-directory, ideally an absolute path. "
                              "The existence of this directory indicates "
                              "ownership of the deploy lock."))
    # These flags are only needed for acquire-lock.
    parser.add_argument('--deployer_email',
                        default='unknown-user@khanacademy.org',
                        help=("The (gmail) email address of the person "
                              "doing the deploy."))
    parser.add_argument('--git_revision',
                        help=("The branch-name (it can also just be a "
                              "commit id) being deployed."))
    parser.add_argument('--auto_deploy',
                        default='false',
                        help=("If 'true', don't ask whether to set the new "
                              "version as the default, do so automatically."))
    parser.add_argument('--jenkins_url',
                        default='http://jenkins.khanacademy.org/',
                        help=("The url of the jenkins server."))
    parser.add_argument('--hipchat_room',
                        default='HipChat Tests',
                        help=("The room to send all hipchat notifications "
                              "to."))
    parser.add_argument('--hipchat_sender',
                        default='Testybot',
                        help=("The name to use as the sender of hipchat "
                              "notifications."))
    parser.add_argument('--deploy_email',
                        default='prod-deploy@khanacademy.org',
                        help=("The AppEngine user to deploy as."))
    parser.add_argument('--deploy_pw_file',
                        default='%s/prod-deploy.pw' % os.environ['HOME'],
                        help=("The file holding deploy_email's "
                              "appengine password."))
    # This is only needed for acquire-lock, but if passed into any other
    # action, the action will ensure the token matches what's in the
    # lockfile before doing anything.
    parser.add_argument('--token',
                        default='',
                        help=("A random string to serve as a unique "
                              "identifier for this deploy."))

    # These flags are only used by set-default.
    parser.add_argument('--monitoring_time', type=int,
                        default=10,
                        help=("How long to monitor in set-default, in "
                              "minutes (0 to disable monitoring)."))
    # This is used to cancel running jobs
    parser.add_argument('--jenkins-build-url',
                        help=("The url of the job that is calling this: "
                              "http://jenkins.khanacademy.org/job/testjob/723/"
                              " or the like"))

    args = parser.parse_args()

    # Make sure the _alert() logging shows up, and have the log-prefix
    # be prettier.
    logging.basicConfig(format="[%(levelname)s] %(message)s")
    logging.getLogger().setLevel(logging.INFO)

    rc = main(args.action, os.path.abspath(args.lockdir),
              acquire_lock_args=(os.path.abspath(args.lockdir),
                                 args.deployer_email,
                                 args.git_revision,
                                 args.auto_deploy == 'true',
                                 _current_gae_version(),
                                 args.jenkins_url,
                                 args.hipchat_room,
                                 args.hipchat_sender,
                                 args.deploy_email,
                                 args.deploy_pw_file,
                                 args.token),
              token=args.token,
              monitoring_time=args.monitoring_time,
              jenkins_build_url=args.jenkins_build_url,
              caller_email=args.deployer_email)
    sys.exit(0 if rc else 1)
