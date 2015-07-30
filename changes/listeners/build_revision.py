from __future__ import absolute_import

import logging

from flask import current_app
from changes.api.build_index import BuildIndexAPIView
from changes.models import ProjectStatus, Project, ProjectOptionsHelper, Revision
from changes.utils.diff_parser import DiffParser
from changes.utils.whitelist import in_project_files_whitelist
from changes.vcs.base import UnknownRevision


def revision_created_handler(revision_sha, repository_id, **kwargs):
    revision = Revision.query.filter(
        Revision.sha == revision_sha,
        Revision.repository_id == repository_id,
    ).first()
    if not revision:
        return

    handler = CommitTrigger(revision)
    handler.run()


class CommitTrigger(object):
    logger = logging.getLogger('build_revision')

    def __init__(self, revision):
        self.repository = revision.repository
        self.revision = revision

    def get_project_list(self):
        return list(Project.query.filter(
            Project.repository_id == self.revision.repository_id,
            Project.status == ProjectStatus.active,
        ))

    def get_changed_files(self):
        vcs = self.repository.get_vcs()
        if not vcs:
            raise NotImplementedError
        # Make sure the repo exists on disk.
        if not vcs.exists():
            vcs.clone()

        diff = None
        try:
            diff = vcs.export(self.revision.sha)
        except UnknownRevision:
            # Maybe the repo is stale; update.
            vcs.update()
            # If it doesn't work this time, we have
            # a problem. Let the exception escape.
            diff = vcs.export(self.revision.sha)

        diff_parser = DiffParser(diff)
        return diff_parser.get_changed_files()

    def run(self):
        revision = self.revision

        project_list = self.get_project_list()
        if not project_list:
            return

        options = ProjectOptionsHelper.get_options(project_list, [
            'build.branch-names',
            'build.commit-trigger',
            'build.file-whitelist',
        ])

        if any(o.get('build.file-whitelist') for o in options.values()):
            files_changed = self.get_changed_files()
        else:
            files_changed = None

        projects_to_build = []
        for project in project_list:
            if options[project.id].get('build.commit-trigger', '1') != '1':
                self.logger.info('build.commit-trigger is disabled for project %s', project.slug)
                continue

            branch_names = filter(bool, options[project.id].get('build.branch-names', '*').split(' '))
            if not revision.should_build_branch(branch_names):
                self.logger.info('No branches matched build.branch-names for project %s', project.slug)
                continue

            if not in_project_files_whitelist(options[project.id], files_changed):
                self.logger.info('No changed files matched build.file-whitelist for project %s', project.slug)
                continue

            projects_to_build.append(project.slug)

        for project_slug in projects_to_build:
            data = {
                'sha': revision.sha,
                'project': project_slug,
                'tag': 'commit',
                'ensure_only': 'true',
            }
            with current_app.test_request_context('/api/0/builds/', method='POST', data=data):
                try:
                    response = BuildIndexAPIView().post()
                except Exception as e:
                    self.logger.exception('Failed to create build: %s' % (e,))
                else:
                    if isinstance(response, (list, tuple)):
                        response, status = response
                        if status != 200:
                            self.logger.error('Failed to create build: %s' % (response,), extra={
                                'data': data,
                            })
