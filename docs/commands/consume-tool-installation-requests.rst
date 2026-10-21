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
are both enabled. The API key must belong to an account in Galaxy's
``admin_users`` that keeps tool installation requests turned on in its
notification preferences.

A scheduled job in your tool list repository can open a pull request with the
requests that pin their tools, and tell those requesters where it is. For
example, in GitHub Actions with ``EPHEMERIS_GALAXY`` and ``EPHEMERIS_API_KEY``
set, and the GitHub CLI::

    consume-tool-installation-requests emit --pin-latest > requests.json
    consume-tool-installation-requests render-yaml --tools-file requested_tools.yaml -o requested_tools.yaml < requests.json
    if ! git diff --quiet; then
        git switch -c "tool-requests-$(date +%s)"
        git commit -am "Install requested tools" && git push -u origin HEAD
        url=$(gh pr create --fill)
        for id in $(jq -r '.[] | select(.action == "pr") | .notification_id' requests.json); do
            consume-tool-installation-requests reply --notification-id "$id" --url "$url" &&
                consume-tool-installation-requests mark-seen "$id"
        done
    fi

Other requests stay unseen, for an admin to read in Galaxy. A reply that fails,
for example because Galaxy lets an account send only 10 notifications a minute,
leaves its request unseen, so the next run tries again. A reply that Galaxy has
not confirmed after a minute counts as failed too, so a requester may get the
same message twice.

Once the tools are installed, for example by ``shed-tools install --tools-file
requested_tools.yaml`` after the merge, ``installed`` prints which of those
requests Galaxy has the pinned revisions of::

    consume-tool-installation-requests installed < requests.json | while read -r id; do
        consume-tool-installation-requests reply --notification-id "$id" --state installed
    done

``render-yaml`` leaves a tool list that gains nothing as it is, and rewrites
one that gains tools without its comments. A tool list it starts asks
``shed-tools`` to install the tools' dependencies, which it otherwise does
not; add ``install_repository_dependencies: true`` and
``install_resolver_dependencies: true`` to an existing one.

Users may ask for tools that nobody has wrapped for Galaxy yet. With
``--wrapper-request-url``, the issue item for such a tool links to a prefilled
request in a tool development repository, for example::

    --wrapper-request-url 'https://github.com/galaxyproject/tools-iuc/issues/new?title={title}&body={body}&labels=tool%20request'

Tools are only looked up on the Tool Shed given with ``--tool-shed-url``, so a
workflow that needs tools from another Tool Shed becomes an issue item. While
the Tool Shed fails, ``emit`` leaves requests for the next run; after a day,
they become issue items anyway.

Work item bodies show what users typed as code, so that it cannot mention
people, and leave out the requester's email address.
