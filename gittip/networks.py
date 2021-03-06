import random

import requests
from aspen import json, log, Response
from aspen.utils import typecheck
from aspen.website import Website
from gittip import db
from psycopg2 import IntegrityError


class RunawayTrain(Exception):
    pass


def claim_id(participant_id):
    """Given a participant_id, return a participant_id.

    If we can claim the given participant_id, we will. Otherwise we'll find a
    random one that isn't taken yet. Whichever we return is guaranteed to be 
    claimed in the database.

    """
    seatbelt = 0
    while 1:
        try:
            db.execute( "INSERT INTO participants (id) VALUES (%s)"
                      , (participant_id,)
                       )
        except IntegrityError:  # Collision, try again with a random value.
            participant_id = hex(int(random.random() * 16**12))[2:].zfill(12)
            seatbelt += 1
            if seatbelt > 100:
                raise RunawayTrain
        else:
            break

    return participant_id


class github:

    @staticmethod
    def upsert(user_info, claim=False):
        return upsert( 'github'
                     , user_info['id']
                     , user_info['login']
                     , user_info
                     , claim=claim
                      )
 

    @staticmethod
    def oauth_url(website, action, then=u""):
        """Given a website object and a string, return a URL string.
        
        `action' is one of 'opt-in', 'lock' and 'unlock'
        
        `then' is either a github username or an URL starting with '/'. It's 
            where we'll send the user after we get the redirect back from 
            GitHub. 

        """
        typecheck(website, Website, action, unicode, then, unicode)
        assert action in [u'opt-in', u'lock', u'unlock']
        url = u"https://github.com/login/oauth/authorize?client_id=%s&redirect_uri=%s" 
        url %= (website.github_client_id, website.github_callback)

        # Pack action,then into data and base64-encode. Querystring isn't
        # available because it's consumed by the initial GitHub request.

        data = u'%s,%s' % (action, then)
        data = data.encode('UTF-8').encode('base64').decode('US-ASCII')
        url += u'?data=%s' % data
        return url


    @staticmethod
    def oauth_dance(website, qs):
        """Given a querystring, return a dict of user_info.

        The querystring should be the querystring that we get from GitHub when
        we send the user to the return value of oauth_url above.

        See also: 

            http://developer.github.com/v3/oauth/

        """

        log("Doing an OAuth dance with Github.") 

        if 'error' in qs:
            raise Response(500, str(qs['error']))

        data = { 'code': qs['code'].encode('US-ASCII')
               , 'client_id': website.github_client_id
               , 'client_secret': website.github_client_secret
                }
        r = requests.post("https://github.com/login/oauth/access_token", data=data)
        assert r.status_code == 200, (r.status_code, r.text)

        back = dict([pair.split('=') for pair in r.text.split('&')]) # XXX
        if 'error' in back:
            raise Response(400, back['error'].encode('utf-8'))
        assert back.get('token_type', '') == 'bearer', back
        access_token = back['access_token']

        r = requests.get( "https://api.github.com/user"
                        , headers={'Authorization': 'token %s' % access_token}
                         )
        assert r.status_code == 200, (r.status_code, r.text)
        user_info = json.loads(r.text)
        log("Done with OAuth dance with Github for %s (%s)." 
            % (user_info['login'], user_info['id']))

        return user_info


    @staticmethod
    def resolve(login):
        """Given two str, return a participant_id.
        """
        FETCH = """\
        
            SELECT participant_id
              FROM social_network_users
             WHERE network='github'
               AND user_info -> 'login' = %s

        """ # XXX Uniqueness constraint on login?
        rec = db.fetchone(FETCH, (login,))
        if rec is None:
            raise Exception("GitHub user %s has no participant." % (login))
        return rec['participant_id']


def upsert(network, user_id, username, user_info, claim=False):
    """Given str, unicode, unicode, and dict, return unicode and boolean.

    Network is the name of a social network that we support (ASCII blah).
    User_id is an immutable unique identifier for the given user on the given
    social network. Username is the user's login/user_id on the given social
    network. We will try to claim that for them here on Gittip. If their
    username is already taken on Gittip then we give them a random one; they
    can change it on their Gittip profile page. User_id and username may or
    may not be the same. User is a dictionary of profile info per the named
    network. All network dicts must have an id key that corresponds to the
    primary key in the underlying table in our own db.

    If claim is True, the return value is the participant_id. Otherwise it is a
    tuple: (participant_id [unicode], is_claimed [boolean], is_locked 
    [boolean], balance [Decimal]).

    """
    typecheck( network, str
             , user_id, (int, unicode)
             , username, unicode
             , user_info, dict
             , claim, bool
              )  
    user_id = unicode(user_id)


    # Record the user info in our database.
    # =====================================

    INSERT = """\
            
        INSERT INTO social_network_users
                    (network, user_id) 
             VALUES (%s, %s)
             
    """ 
    try:
        db.execute(INSERT, (network, user_id,))
    except IntegrityError:
        pass  # That login is already in our db.
    
    UPDATE = """\
            
        UPDATE social_network_users
           SET user_info=%s
         WHERE user_id=%s 
     RETURNING participant_id

    """
    for k, v in user_info.items():
        # Cast everything to unicode. I believe hstore can take any type of 
        # value, but psycopg2 can't. 
        # https://postgres.heroku.com/blog/past/2012/3/14/introducing_keyvalue_data_storage_in_heroku_postgres/
        # http://initd.org/psycopg/docs/extras.html#hstore-data-type
        user_info[k] = unicode(v)
    rec = db.fetchone(UPDATE, (user_info, user_id))


    # Find a participant.
    # ===================
    
    if rec is not None and rec['participant_id'] is not None:

        # There is already a Gittip participant associated with this account.

        participant_id = rec['participant_id']
        new_participant = False

    else:

        # This is the first time we've seen this user. Let's create a new
        # participant for them, claiming their user_id for them if possible.

        participant_id = claim_id(username)
        new_participant = True


    # Associate the social network user with the Gittip participant.
    # ================================================================

    ASSOCIATE = """\
            
        UPDATE social_network_users
           SET participant_id=%s
         WHERE network=%s
           AND user_id=%s
           AND (  (participant_id IS NULL)
               OR (participant_id=%s)
                 )
     RETURNING participant_id, is_locked

    """

    log(u"Associating %s (%s) on %s with %s on Gittip." 
        % (username, user_id, network, participant_id))
    rows = db.fetchall( ASSOCIATE
                      , (participant_id, network, user_id, participant_id)
                       )
    rows = list(rows)
    nrows = len(rows)
    assert nrows in (0, 1)

    if nrows == 1:
        is_locked = rows[0]['is_locked']
    else:

        # Against all odds, the account was otherwise associated with another
        # participant while we weren't looking. Maybe someone paid them money
        # at *just* the right moment. If we created a new participant then back
        # that out.

        if new_participant:
            db.execute( "DELETE FROM participants WHERE id=%s"
                      , (participant_id,)
                       )

        rec = db.fetchone( "SELECT participant_id, is_locked "
                           "FROM social_network_users "
                           "WHERE network=%s AND user_id=%s" 
                         , (network, user_id)
                          )
        if rec is not None:

            # Use the participant associated with this account.

            participant_id = rec['participant_id']
            is_locked = rec['is_locked']
            assert participant_id is not None

        else:

            # Okay, now this is just screwy. The participant disappeared right
            # at the last moment! Log it and fail.

            raise Exception("We're bailing on associating %s user %s (%s) with"
                            " a Gittip participant." 
                            % (network, username, user_id))


    # Record the participant as claimed if asked to.
    # ==============================================

    if claim:
        CLAIM = """\

            UPDATE participants 
               SET claimed_time=CURRENT_TIMESTAMP
             WHERE id=%s 
               AND claimed_time IS NULL

        """
        db.execute(CLAIM, (participant_id,))
        out = participant_id
    else:
        rec = db.fetchone( "SELECT claimed_time, balance FROM participants "
                           "WHERE id=%s"
                         , (participant_id,)
                          )
        assert rec is not None
        out = ( participant_id
              , rec['claimed_time'] is not None
              , is_locked
              , rec['balance']
               )

    return out
