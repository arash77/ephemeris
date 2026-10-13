Consume-tool-installation-requests
==================================

.. automodule :: ephemeris.tool_request_consumer

Usage
----------

.. argparse::
   :module: ephemeris.tool_request_consumer
   :func: _parser
   :prog: consume-tool-installation-requests

Running it on a schedule
------------------------

Galaxy sends tool installation requests to its admins when
``enable_tool_installation_request_form`` and ``enable_notification_system``
are both enabled. The script below checks for new requests, opens an issue or
a pull request (a merge request on GitLab) for each of them in your tool list
repository, and tells the requester where it is handled. It works with any
Galaxy server, on GitHub and GitLab, from any CI system or cron job that runs
it in a clean checkout of the repository. It needs bash, git, curl, jq and
ephemeris, and its first lines list the variables it reads.

What pull requests change depends on how your repository lists its tools:

- Without ``TOOLS_FILE`` and ``APPLY_COMMAND``, every request becomes an issue.
  This works with any repository.
- With ``TOOLS_FILE``, pull requests add the requested tools to that tool list
  with ``render-yaml``.
- With ``APPLY_COMMAND``, pull requests hold what that shell command changes.
  It runs in the checkout, on a new branch, with ``$ITEM`` naming a JSON file
  that holds the work item from ``emit``: ``notification_id``, ``action``,
  ``title``, ``body``, and ``tools``, whose tool list entries have ``name``,
  ``owner``, ``tool_shed_url``, ``tool_panel_section_label`` and
  ``revisions``. For example, a repository with a tool list per section and
  lock files could add each tool to the tool list of its section, and its
  revisions to the lock file. If the command changes nothing, the request
  becomes an issue.

On GitHub:

- copy ``tool-installation-requests.sh`` to ``.github/`` and the workflow
  below to ``.github/workflows/``,
- set ``EPHEMERIS_GALAXY`` to your Galaxy server, and ``TOOLS_FILE`` or
  ``APPLY_COMMAND`` as above,
- add the API key of an admin account as the ``GALAXY_ADMIN_API_KEY``
  repository secret,
- enable "Allow GitHub Actions to create and approve pull requests" in the
  repository's Actions settings.

Pull requests opened with the default ``GITHUB_TOKEN`` do not start other
workflows. To run your tool installation tests on them, put a GitHub App or
personal access token in ``FORGE_TOKEN`` instead.

On GitLab:

- copy ``tool-installation-requests.sh`` next to ``.gitlab-ci.yml``, and add
  the job below to ``.gitlab-ci.yml``,
- set ``EPHEMERIS_GALAXY``, and ``TOOLS_FILE`` or ``APPLY_COMMAND``,
- add the API key of an admin account as the masked ``EPHEMERIS_API_KEY``
  CI/CD variable, and a project access token with the ``api`` and
  ``write_repository`` scopes and at least the Developer role as the masked
  ``FORGE_TOKEN`` variable, since the CI job token cannot push branches,
- create a pipeline schedule, for example every hour.

``emit`` only picks up requests that are not marked as seen. Use an admin
account that nobody reads notifications with, since marking them as read in
Galaxy hides them from the script.

A request is marked as seen once its requester got an answer, or cannot get
one because their account is gone or they turned off message notifications.
If a run stops halfway, for example because Galaxy limits how many
notifications an account may send per minute (``send_notification_rate_limit``),
the next run answers the remaining requests and reuses the issues and pull
requests already opened. It finds pull requests by their branch, and issues by
the ``ISSUE_LABEL`` label and the first line of their body. A pull request item
whose tools are in the repository already becomes an issue instead.

Request pull requests all start from the same base branch, so merging one can
make others conflict, like any pull requests that change the same lines. An
``APPLY_COMMAND`` that inserts tools in sorted order, instead of at the end of
a file, avoids most of these conflicts.

``render-yaml`` leaves a tool list that gains nothing as it is, and rewrites
one that gains tools without its comments. Like ``shed-tools``, it reads an
entry with neither ``revisions`` nor ``changeset_revision`` as following the
latest revision, and leaves that repository alone. An entry pinned with
``changeset_revision`` that gains a revision gets a ``revisions`` list instead.

Users may ask for tools that nobody has wrapped for Galaxy yet. With
``--wrapper-request-url``, the issue for a tool that no Tool Shed repository is
named like links to a prefilled request in a tool development repository, for
an admin to check and send. ``{name}``, ``{url}``, ``{description}``,
``{domain}``, ``{title}`` and ``{body}`` in the URL are replaced with what the
user said about the tool. For example, set ``EMIT_OPTIONS`` to one of::

    --wrapper-request-url 'https://github.com/galaxyproject/tools-iuc/issues/new?title={title}&body={body}&labels=tool%20request'
    --wrapper-request-url 'https://github.com/bgruening/galaxytools/issues/new?template=tool-request.yml&title={title}&tool-name={name}&description={description}&links={url}'

Tools are only looked up on the Tool Shed given to ``emit``. A workflow that needs tools from
another Tool Shed leads to an issue, so that an admin decides whether to trust
that Tool Shed. While that Tool Shed fails to answer, ``emit`` leaves requests
for the next run; after a day, they become issues anyway.

Issue and pull request bodies show what users typed into the request form as
code, so that it cannot mention people. They do not contain the requester's
email address.

.. literalinclude:: ../examples/tool-installation-requests.sh
   :language: bash

The GitHub Actions workflow:

.. literalinclude:: ../examples/tool-installation-requests.yml
   :language: yaml

The GitLab CI job:

.. literalinclude:: ../examples/tool-installation-requests.gitlab-ci.yml
   :language: yaml
