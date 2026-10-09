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
are both enabled. The GitHub Actions workflow below checks for new requests
every hour, opens an issue or a pull request for each of them in your tool list
repository, and tells the requester where it is handled. To use it:

- copy it to ``.github/workflows/`` in the tool list repository,
- set ``EPHEMERIS_GALAXY`` to your Galaxy server and ``TOOLS_FILE`` to the tool
  list that pull requests add tools to,
- add the API key of an admin account as the ``GALAXY_ADMIN_API_KEY``
  repository secret,
- enable "Allow GitHub Actions to create and approve pull requests" in the
  repository's Actions settings.

Pull requests opened with the default ``GITHUB_TOKEN`` do not start other
workflows. To run your tool installation tests on them, put a GitHub App or
personal access token in ``GH_TOKEN`` instead.

``emit`` only picks up requests that are not marked as seen. Use an admin
account that nobody reads notifications with, since marking them as read in
Galaxy hides them from the workflow.

A request is marked as seen only once its requester got an answer. If a run
stops halfway, for example because Galaxy limits how many notifications an
account may send per minute (``send_notification_rate_limit``), the next run
answers the remaining requests and reuses the issues and pull requests already
opened. A pull request item whose revisions are in the tool list already
becomes an issue instead.

``render-yaml`` rewrites the tool list without its comments, and leaves
entries without ``revisions`` alone, since those already follow the latest
revision. If you keep pinned revisions in lock files, point ``TOOLS_FILE`` at
one of those.

Tools are only looked up on the Tool Shed given to ``emit``. A workflow that needs tools from
another Tool Shed leads to an issue, so that an admin decides whether to trust
that Tool Shed.

Issue and pull request bodies show what users typed into the request form as
code, so that it cannot mention people. They do not contain the requester's
email address.

.. literalinclude:: ../examples/tool-installation-requests.yml
   :language: yaml

The same commands work in a scheduled GitLab pipeline, using ``glab`` or the
GitLab API instead of ``gh`` to open issues and merge requests.
