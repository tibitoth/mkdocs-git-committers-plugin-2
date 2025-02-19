import os
import logging
from timeit import default_timer as timer
from datetime import datetime

from mkdocs import utils as mkdocs_utils
from mkdocs.config import config_options
from mkdocs.plugins import BasePlugin

from git import Repo, Commit
import requests, json
from requests.exceptions import HTTPError
import time

LOG = logging.getLogger("mkdocs.plugins." + __name__)

class GitCommittersPlugin(BasePlugin):

    config_scheme = (
        ('github_token', config_options.Type(str, default='')),
        ('enterprise_hostname', config_options.Type(str, default='')),
        ('repository', config_options.Type(str, default='')),
        ('branch', config_options.Type(str, default='master')),
        ('docs_path', config_options.Type(str, default='docs/')),
        ('enabled', config_options.Type(bool, default=True)),
        ('cache_dir', config_options.Type(str, default='.cache/plugin/git-committers')),
    )

    def __init__(self):
        self.total_time = 0
        self.branch = 'master'
        self.enabled = True
        self.authors = dict()
        self.cache_page_authors = dict()
        self.cache_date = ''

    def on_config(self, config):
        self.enabled = self.config['enabled']
        if not self.enabled:
            LOG.info("git-committers plugin DISABLED")
            return config

        LOG.info("git-committers plugin ENABLED")

        if not self.config['repository']:
            LOG.error("git-committers plugin: repository not specified")
            return config
        if self.config['enterprise_hostname'] and self.config['enterprise_hostname'] != '':
            self.githuburl = "https://" + self.config['enterprise_hostname'] + "/"
        else:
            self.githuburl = "https://github.com/"
        self.github_avatar_url = 'https://avatars.githubusercontent.com/'
        self.localrepo = Repo(".")
        self.branch = self.config['branch']
        self.github_token = self.config['github_token']
        return config

    def list_contributors(self, path, page):
        graphquery = """
{{
  repository(owner: \\\"{org}\\\", name: \\\"{repo}\\\") {{
    # branch name
    ref(qualifiedName:\\\"{branch}\\\") {{      
      target {{
        # cast Target to a Commit
        ... on Commit {{
          # full repo-relative path to blame file
          blame(path:\\\"{path}\\\") {{
            ranges {{
              commit {{
                author {{
                  user {{
                    avatarUrl
                    login
                    name
                  }}
                }}
              }}
              age
            }}
          }}
        }}
      }}
    }}
  }}
}}"""

        last_commit_date = ""
        for c in Commit.iter_items(self.localrepo, self.localrepo.head, path):
            if not last_commit_date:
                # Use the last commit and get the date
                last_commit_date = time.strftime("%Y-%m-%d", time.gmtime(c.authored_date))

        # File not committed yet
        if last_commit_date == "":
            last_commit_date = datetime.now().strftime("%Y-%m-%d")
            return [], last_commit_date

        # Try to leverage the cache
        if path in self.cache_page_authors:
            if self.cache_date and time.strptime(last_commit_date, "%Y-%m-%d") < time.strptime(self.cache_date, "%Y-%m-%d"):
                return self.cache_page_authors[path]['authors'], self.cache_page_authors[path]['last_commit_date']
        
        manual_authors = []
        if 'authors' in page.meta:
            users = page.meta['authors'].split(',')
            for username in users:
                manual_authors.append({'login': username, 'name': username, 'url': self.githuburl + username, 'avatar': self.github_avatar_url + username})

        blame_authors=[]
        url_graphql = "https://api.github.com/graphql"
        LOG.info("git-committers: fetching contributors for " + path)
        try:
            jsonBody = "{ \"query\": \"" + graphquery.format(
                    org=self.config['repository'].split('/')[0],
                    repo=self.config['repository'].split('/')[1], 
                    branch=self.branch,
                    path=path).replace("\n", "\\n") + "\"}"
            LOG.debug("git-committers: graphql query: " + jsonBody)
            response = requests.post(url_graphql, data = jsonBody, headers={ 'Authorization': 'Bearer ' + self.github_token })
            response.raise_for_status()
        except HTTPError as http_err:
            LOG.error(f'git-committers: HTTP error occurred: {http_err}\n(404 is normal if file is not on GitHub yet or Git submodule)')
            LOG.debug(f'error body: {response.text}')
        except Exception as err:
            LOG.error(f'git-committers: Other error occurred: {err}')
        else:
            jsonBody = response.text
            # Parse the json response
            data = json.loads(jsonBody)
            # get author logins and avatars based on the graphquery's response
            for commit in data['data']['repository']['ref']['target']['blame']['ranges']:
                user = commit['commit']['author']['user']
                if user is None:
                    continue
                
                login = user['login']
                name = user['name']
                url = self.githuburl + login
                avatar = user['avatarUrl']
                if any(x['login'] == login for x in blame_authors) == False and any(x['login'] == login for x in manual_authors) == False:
                    blame_authors.append({'login':login, 'name': name, 'url': url, 'avatar': avatar})

            blame_authors.sort(key = lambda x: x['login'].lower())

        authors = manual_authors + blame_authors

        # Update global cache_page_authors
        self.cache_page_authors[path] = {'last_commit_date': last_commit_date, 'authors': authors}

        return authors, last_commit_date

    def on_page_context(self, context, page, config, nav):
        context['committers'] = []
        if not self.enabled:
            return context
        start = timer()
        git_path = self.config['docs_path'] + page.file.src_path
        authors, last_commit_date = self.list_contributors(git_path, page)
        if authors:
            context['committers'] = authors
        if last_commit_date:
            context['last_commit_date'] = last_commit_date
        end = timer()
        self.total_time += (end - start)

        return context

    def on_post_build(self, config):
        LOG.info("git-committers: saving page authors cache file")
        json_data = json.dumps({'cache_date': datetime.now().strftime("%Y-%m-%d"), 'page_authors': self.cache_page_authors})
        os.makedirs(self.config['cache_dir'], exist_ok=True)
        f = open(self.config['cache_dir'] + "/page-authors.json", "w")
        f.write(json_data)
        f.close()

    def on_pre_build(self, config):
        if os.path.exists(self.config['cache_dir'] + "/page-authors.json"):
            LOG.info("git-committers: found page authors cache file - loading it")
            f = open(self.config['cache_dir'] + "/page-authors.json", "r")
            cache = json.loads(f.read())
            self.cache_date = cache['cache_date']
            self.cache_page_authors = cache['page_authors']
            f.close()
