"""Utility functions for the plugins table."""

import logging
import random

import rethinkdb as r
from slugify import slugify

import db.util

r_conn = db.util.r_conn


class RequiredProperty(object):
    pass


_ROW_SCHEMA = {

    # Primary key. Human-readable permalink for a plugin. Eg. 'python-2'
    'slug': RequiredProperty(),

    # A name used strictly for purposes of associating info from different
    # sources together. Eg. "nerdtree" (instead of "the-NERD-Tree.vim")
    'normalized_name': '',

    # eg. ['C/C++', 'autocomplete']
    'tags': [],

    # Unix timestamp in seconds
    'created_at': 0,
    'updated_at': 0,

    ###########################################################################
    # Info from the script on vim.org.
    # eg. http://www.vim.org/scripts/script.php?script_id=2736

    # Eg. '1234' (string)
    'vimorg_id': None,

    # Eg. 'Syntastic'
    'vimorg_name': '',

    # Eg. 'Martin Grenfell'
    'vimorg_author': '',

    # eg. 'http://www.vim.org/scripts/script.php?script_id=2736'
    'vimorg_url': '',

    # eg. 'utility'
    'vimorg_type': '',

    'vimorg_rating': 0,
    'vimorg_num_raters': 0,
    'vimorg_downloads': 0,
    'vimorg_short_desc': '',
    'vimorg_long_desc': '',
    'vimorg_install_details': '',

    ###########################################################################
    # Info from the author's GitHub repo (eg. github.com/scrooloose/syntastic)

    # eg. 'scrooloose'
    'github_owner': '',

    # eg. 'syntastic'
    'github_repo_name': '',

    'github_stars': 0,

    # eg. 'Syntax checking hacks for vim'
    'github_short_desc': '',

    # eg. 'http://valloric.github.io/YouCompleteMe/'
    'github_homepage': '',

    # TODO(david): Need to store filetype (eg. Markdown, plain, reST)
    'github_readme': '',

    ###########################################################################
    # Info from the github.com/vim-scripts mirror.
    # eg. github.com/vim-scripts/Syntastic

    # Eg. 'syntastic'
    'github_vim_scripts_repo_name': '',

    'github_vim_scripts_stars': 0,

    ###########################################################################
    # Info derived from elsewhere

    # Number of Vundle/Pathogen/NeoBundle etc. users that reference the
    # author's GitHub repo.
    'github_bundles': 0,

    # Number of Vundle/Pathogen/NeoBundle etc. users that reference the
    # vim-scripts GitHub mirror.
    'github_vim_scripts_bundles': 0,

}


###############################################################################
# Routines for basic DB CRUD operations.


def ensure_table():
    db.util.ensure_table('plugins', primary_key='slug')

    db.util.ensure_index('plugins', 'vimorg_id')
    db.util.ensure_index('plugins', 'github_stars')


# TODO(david): Yep, using an ODM enforcing a consistent schema on write AND
#     read would be great.
def insert(plugins, *args, **kwargs):
    """Insert or update a plugin or list of plugins.

    Although this would be more accurately named "upsert", this is a wrapper
    around http://www.rethinkdb.com/api/python/#insert that ensures
    a consistent plugin schema before inserting into DB.
    """
    if not isinstance(plugins, list):
        plugins = [plugins]

    mapped_plugins = []
    for plugin in plugins:

        # Generate a unique slug if not already present.
        if not plugin.get('slug'):
            plugin['slug'] = _generate_unique_slug(plugin)

        # FIXME(david): This is scaffolding code.
        if not plugin.get('normalized_name'):
            plugin['normalized_name'] = 'meredith grey swift'

        mapped_plugins.append(dict(_ROW_SCHEMA, **plugin))

    return r.table('plugins').insert(mapped_plugins, *args, **kwargs).run(
            r_conn())


def _generate_unique_slug(plugin):
    """Create a unique, human-readable ID for this plugin that can be used in
    a permalink URL.

    WARNING: Not thread-safe.
    """
    name = (plugin.get('vimorg_name') or plugin.get('github_repo_name') or
            plugin.get('github_vim_scripts_repo_name'))
    assert name

    slug = slugify(name)
    if not _slug_taken(slug):
        return slug

    # If the slug isn't unique, try appending different slug suffixes until we
    # get a unique slug. Don't worry, these suffixes only show up in the URL.
    # And it's more efficient to randomly permute these than using
    # a monotonically increasing integer.
    slug_suffixes = [
        'all-too-well',
        'back-to-december',
        'better-than-revenge',
        'come-back-be-here',
        'enchanted',
        'fearless',
        'holy-ground',
        'long-live',
        'love-story',
        'mine',
        'ours',
        'red',
        'safe-and-sound',
        'sparks-fly',
        'state-of-grace',
        'sweeter-than-fiction',
        'treacherous',
        'you-belong-with-me',
    ]
    random.shuffle(slug_suffixes)

    for slug_suffix in slug_suffixes:
        slug = slugify('%s-%s' % (name, slug_suffix))

        if not _slug_taken(slug):
            return slug

    raise Exception('Uh oh, we need more song titles. Too many'
            ' collisions of %s' % name)


def _slug_taken(slug):
    """Returns whether a slug has already been used."""
    return bool(r.table('plugins').get(slug).run(r_conn()))


# FIXME(david): This will be going away (using the new 'slug' primary key).
def get_for_name(name):
    """Get the plugin model of the given name."""
    return db.util.get_first(r.table('plugins').get_all(name, index='name'))


def update_tags(plugin, tags):
    """Updates a plugin's tags to the given set, and updates aggregate tag
    counts.
    """
    plugin_tags = plugin['tags']
    added_tags = set(tags) - set(plugin_tags)
    removed_tags = set(plugin_tags) - set(tags)

    # TODO(david): May have to hold a lock while doing this
    map(db.tags.add_tag, added_tags)
    map(db.tags.remove_tag, removed_tags)

    plugin['tags'] = tags
    r.table('plugins').update(plugin).run(r_conn())


###############################################################################
# Routines for merging in data from scraped sources.
# TODO(david): Write a Craig-esque comment about how all this works.
# TODO(david): Make most of these functions private once we get rid of
#     db_upsert.py.


def is_more_authoritative(repo1, repo2):
    """Returns whether repo1 is a different and more authoritative GitHub repo
    about a certain plugin than repo2.

    For example, the original author's GitHub repo for Syntastic
    (https://github.com/scrooloose/syntastic) is more authoritative than
    vim-scripts's mirror (https://github.com/vim-scripts/Syntastic).
    """
    # If we have two different GitHub repos, take the latest updated, and break
    # ties by # of stars.
    if (repo1.get('github_url') and repo2.get('github_url') and
            repo1['github_url'] != repo2['github_url']):
        if repo1.get('updated_at', 0) > repo2.get('updated_at', 0):
            return True
        elif repo1.get('updated_at', 0) == repo2.get('updated_at', 0):
            return repo1.get('github_stars', 0) > repo2.get('github_stars', 0)
        else:
            return False
    else:
        return False


def update_plugin(old_plugin, new_plugin):
    """Merges properties of new_plugin onto old_plugin, much like a dict
    update.

    This is used to reconcile differences of data that we might get from
    multiple sources about the same plugin, such as from vim.org, vim-scripts
    GitHub repo, and the author's original GitHub repo.

    Does not mutate any arguments. Returns the updated plugin.
    """
    # If the old_plugin is constituted from information from a more
    # authoritative GitHub repo (eg. the author's) than new_plugin, then we
    # want to use old_plugin's data where possible.
    if is_more_authoritative(old_plugin, new_plugin):
        updated_plugin = _merge_dict_except_none(new_plugin, old_plugin)
    else:
        updated_plugin = _merge_dict_except_none(old_plugin, new_plugin)

    # Keep the latest updated date.
    if old_plugin.get('updated_at') and new_plugin.get('updated_at'):
        updated_plugin['updated_at'] = max(old_plugin['updated_at'],
                new_plugin['updated_at'])

    # Keep the earliest created date.
    if old_plugin.get('created_at') and new_plugin.get('created_at'):
        updated_plugin['created_at'] = min(old_plugin['created_at'],
                new_plugin['created_at'])

    return updated_plugin


def _merge_dict_except_none(dict_a, dict_b):
    """Returns dict_a updated with any key/value pairs from dict_b where the
    value is not None.

    Does not mutate arguments. Also, please don't drink and drive.
    """
    dict_b_filtered = {k:v for k, v in dict_b.iteritems() if v is not None}
    return dict(dict_a, **dict_b_filtered)


def _find_matching_vimorg_plugins(plugin_data, repo=None):
    """Attempts to find the matching vim.org plugin from the given data using
    various heuristics.

    Ideally, this would never return more than one matching plugin, but our
    heuristics are not perfect and there are many similar vim.org plugins named
    "python.vim," for example.

    Arguments:
        plugin_data: Scraped data about a plugin.
        repo: (optional) If plugin_data is scraped from GitHub, the
            corresponding github_repo document containing info about the GitHub
            repo.

    Returns:
        A list of plugins that are likely to be the same as the given
        plugin_data based on vim.org data.
    """
    # If we have a vimorg_id, then we have a direct key to a vim.org script
    # if it's in DB.
    if plugin_data.get('vimorg_id'):
        query = r.table('plugins').get_all(plugin_data['vimorg_id'],
                index='vimorg_id')
        return list(query.run(r_conn()))

    # FIXME(david): Handle no vimorg_id case (this is the infamous "associate
    #     github repo with vim.org script" algorithm)


def add_scraped_data(plugin_data, repo=None):
    """Adds scraped plugin data from either vim.org, a github.com/vim-scripts
    repo, or an arbitrary GitHub repo.

    This will attempt to match the plugin data with an existing plugin already
    in the DB using various heuristics. If found a reasonable match is found,
    we update, else, we insert a new plugin.

    Arguments:
        plugin_data: Scraped data about a plugin.
        repo: (optional) If plugin_data is scraped from GitHub, the
            corresponding github_repo document containing info about the GitHub
            repo.
    """
    plugins = _find_matching_vimorg_plugins(plugin_data)

    if not plugins:
        insert(plugin_data)
    elif len(plugins) == 1:
        updated_plugin = update_plugin(plugins[0], plugin_data)
        insert(updated_plugin, upsert=True)
    else:
        logging.error(
                'Uh oh, we found %s plugins that match the scraped data:\n'
                'Scraped data: %s\nMatching plugin slugs: %s' % (
                len(plugins), plugin_data, [p['slug'] for p in plugins]))


###############################################################################
# Utility functions for powering the web search.


def get_search_index():
    """Returns a view of the plugins table that can be used for search.

    More precisely, we return a sorted list of all plugins, with fields limited
    to the set that need to be displayed in search results or needed for
    filtering and sorting. A keywords field is added that can be matched on
    user-given search keywords.

    We perform a search on plugins loaded in-memory because this is a lot more
    performant (20x-30x faster on my MBPr) than ReQL queries, and the ~5000
    plugins fit comfortably into memory.

    The return value of this function should be cached for these gains.
    """
    query = r.table('plugins')

    # FIXME(david): These fields need to change.
    query = query.pluck(['id', 'name', 'created_at', 'updated_at', 'tags',
        'homepage', 'author', 'vimorg_id', 'vimorg_rating',
        'vimorg_short_desc', 'github_stars', 'github_url', 'github_short_desc',
        'plugin_manager_users'])

    plugins = list(query.run(r_conn()))

    # We can't order_by on multiple fields with secondary indexes due to the
    # following RethinkDB bug: https://github.com/rethinkdb/docs/issues/160
    # Thus, we sort in-memory for now because it's way faster than using
    # Rethink's order_by w/o indices (~7 secs vs. ~0.012 secs on my MBPr).
    # TODO(david): Pass sort ordering as an argument somehow.
    plugins.sort(key=lambda p: (-p.get('plugin_manager_users', 0),
            -p.get('github_stars', 0), -p.get('vimorg_rating', 0)))

    for plugin in plugins:
        tokens = _get_search_tokens_for_plugin(plugin)
        plugin['keywords'] = ' '.join(tokens)

    return plugins


def _get_search_tokens_for_plugin(plugin):
    """Returns a set of lowercased keywords generated from various fields on
    the plugin that can be used for searching.
    """
    search_fields = ['name', 'tags', 'author', 'vimorg_short_desc',
            'github_short_desc']
    tokens = set()

    for field in search_fields:

        if field not in plugin:
            continue

        value = plugin[field]
        if isinstance(value, basestring):
            tokens_list = value.split()
        elif isinstance(value, list):
            tokens_list = value
        elif value is None:
            tokens_list = []
        else:
            raise Exception('Field %s has untokenizable type %s' % (
                field, type(value)))

        tokens |= set(t.lower() for t in tokens_list)

    return tokens
