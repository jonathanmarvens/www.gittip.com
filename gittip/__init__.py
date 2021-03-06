import datetime
from decimal import Decimal

BIRTHDAY = datetime.date(2012, 6, 5)
def age():
    age = datetime.date.today() - BIRTHDAY
    return "%d days" % age.days


db = None # This global is wired in wireup. It's an instance of 
          # gittip.postgres.PostgresManager.
AMOUNTS= [Decimal(a) for a in ('0.00', '0.08', '0.16', '0.32', '0.64', '1.28')]


__version__ = "~~VERSION~~"


def get_tip(tipper, tippee):
    """Given two user ids, return a Decimal.
    """
    TIP = """\
            
        SELECT amount 
          FROM tips 
         WHERE tipper=%s 
           AND tippee=%s 
      ORDER BY mtime DESC
         LIMIT 1

    """
    rec = db.fetchone(TIP, (tipper, tippee))
    if rec is None:
        tip = Decimal(0.00)
    else:
        tip = rec['amount']
    return tip


def get_backed_amount(participant_id):
    """Given a unicode, return a Decimal. 
    """

    BACKED = """\

        SELECT sum(amount) AS backed
          FROM ( SELECT DISTINCT ON (tipper)
                        amount
                      , tipper
                   FROM tips
                   JOIN participants p ON p.id = tipper
                  WHERE tippee=%s
                    AND last_bill_result = ''
               ORDER BY tipper
                      , mtime DESC
                ) AS foo

    """
    rec = db.fetchone(BACKED, (participant_id,))
    if rec is None:
        amount = None
    else:
        amount = rec['backed']  # might be None

    if amount is None:
        amount = Decimal(0.00)

    return amount


def get_tipjar(participant_id, pronoun="their", claimed=False):
    """Given a participant id, return a unicode.
    """

    amount = get_backed_amount(participant_id)


    # Compute a unicode describing the amount.
    # ========================================
    # This is down here because we use it in multiple places in the app,
    # basically in the claimed participant page and the non-claimed GitHub page
    # (hopefully soon to be Facebook and Twitter as well).

    if pronoun not in ('your', 'their'):
        raise Exception("Unknown, um, pronoun: %s" % pronoun)

    if amount == 0:
        if pronoun == "your":
            tipjar = u"have no backed tips."
        elif pronoun == "their":
            tipjar = u"has no backed tips."
    else:
        if pronoun == "your":
            tipjar = u"have $%s in backed tips."
        elif pronoun == "their":
            tipjar = u"has $%s in backed tips."
        tipjar %= amount


    # We're opt-in.
    # =============
    # If the user hasn't claimed the tipjar then the tips are only pledges, 
    # we're not going to actually collect money on their behalf.
   
    if not claimed:
        tipjar = tipjar.replace("backed", "pledged")


    return tipjar


def get_tips_and_total(tipper, for_payday=False):
    """Given a participant id and a date, return a list and a Decimal.

    This function is used to populate a participant's page for their own
    viewing pleasure, and also by the payday function. If for_payday is not
    False it must be a date object.

    """
    if for_payday:

        # For payday we want the oldest relationship to be paid first.
        order_by = "ctime ASC"


        # This is where it gets crash-proof.
        # ==================================
        # We need to account for the fact that we may have crashed during
        # Payday and we're re-running that function. We only want to select
        # tips that existed before Payday started, but haven't been processed
        # as part of this Payday yet.
        # 
        # It's a bug if the paydays subselect returns > 1 rows.
        # 
        # XXX If we crash during Payday and we rerun it after a timezone 
        # change, will we get burned? How?

        ts_filter = """\

               AND mtime < %s
               AND ( SELECT id 
                       FROM transfers
                      WHERE tipper=t.tipper
                        AND tippee=t.tippee
                        AND timestamp >= %s
                    ) IS NULL
                 
        """
        args = (tipper, for_payday, for_payday)
    else:
        order_by = "amount DESC"
        ts_filter = ""
        args = (tipper,)

    TIPS = """\

        SELECT * FROM (
            SELECT DISTINCT ON (tippee) 
                   amount
                 , tippee
                 , t.ctime
                 , p.claimed_time
              FROM tips t
              JOIN participants p ON p.id = t.tippee
             WHERE tipper = %%s
               %s
          ORDER BY tippee
                 , t.mtime DESC
        ) AS foo
        ORDER BY %s
               , tippee

    """ % (ts_filter, order_by)  # XXX, No injections here, right?!
    tips = list(db.fetchall(TIPS, args))


    # Compute the total.
    # ==================
    # For payday we only want to process payments to tippees who have
    # themselves opted into Gittip. For the tipper's profile page we want to
    # show the total amount they've pledged (so they're not surprised when
    # someone *does* start accepting tips and all of a sudden they're hit with
    # bigger charges.

    if for_payday:
        to_total = [t for t in tips if t['claimed_time'] is not None]
    else:
        to_total = tips
    total = sum([t['amount'] for t in to_total])

    if not total:  # XXX Why is this necessary?
        total = Decimal('0.00')

    return tips, total


# canonizer
# =========
# This is an Aspen hook to ensure that requests are served on a certain root
# URL, even if multiple domains point to the application.

class X: pass
canonical_scheme = None
canonical_host = None

def canonize(request):
    """Enforce a certain scheme and hostname. Store these on request as well.
    """
    scheme = request.headers.get('X-Forwarded-Proto', 'http') # per Heroku
    host = request.headers['Host']
    bad_scheme = scheme != canonical_scheme
    bad_host = bool(canonical_host) and (host != canonical_host) 
                # '' and False => ''
    if bad_scheme or bad_host:
        url = '%s://%s/' % (canonical_scheme, canonical_host)
        request.redirect(url, permanent=True)
