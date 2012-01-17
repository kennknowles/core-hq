import pytz
from pytz import timezone
from datetime import timedelta, datetime, date
import re
from couchdbkit.ext.django.schema import *
from django.conf import settings
from casexml.apps.case.models import CommCareCase
from corehq.apps.sms.api import send_sms
from corehq.apps.users.models import CommCareUser
import logging
from dimagi.utils.parsing import string_to_datetime, json_format_datetime

def is_true_value(val):
    return val == 'ok' or val == 'OK'

class MessageVariable(object):
    def __init__(self, variable):
        self.variable = variable

    def __unicode__(self):
        return unicode(self.variable)

    @property
    def days_until(self):
        try: variable = string_to_datetime(self.variable)
        except Exception:
            return "(?)"
        else:
            # add 12 hours and then floor == round to the nearest day
            return (variable - datetime.utcnow() + timedelta(hours=12)).days

    def __getattr__(self, item):
        try:
            return super(MessageVariable, self).__getattribute__(item)
        except Exception:
            pass
        try:
            return MessageVariable(getattr(self.variable, item))
        except Exception:
            pass
        try:
            return MessageVariable(self.variable[item])
        except Exception:
            pass
        return "(?)"

class Message(object):
    def __init__(self, template, **params):
        self.template = template
        self.params = {}
        for key, value in params.items():
            self.params[key] = MessageVariable(value)
    def __unicode__(self):
        return self.template.format(**self.params)

    @classmethod
    def render(cls, template, **params):
        if isinstance(template, str):
            template = unicode(template, encoding='utf-8')
        return unicode(cls(template, **params))
    
METHOD_CHOICES = ["sms", "email", "test", "callback", "callback_test"]

"""
A CaseReminderEvent describes a single instance when a text message should be sent.

day_num                     The day offset from the beginning of the current event list iteration for when this event should fire (starting with 0)
fire_time                   The time of day when this event should fire
message                     The text to send along with language to send it, represented as a dictionary: {"en" : "Hello"}
callback_timeout_intervals  For CaseReminderHandlers whose method is "callback", a list of timeout intervals (in minutes); The message is resent based on the number of entries in this list until the callback is received, or the number of timeouts is exhausted
"""
class CaseReminderEvent(DocumentSchema):
    day_num = IntegerProperty()
    fire_time = TimeProperty()
    message = DictProperty()
    callback_timeout_intervals = ListProperty(IntegerProperty)

"""
A CaseReminderCallback object logs the callback response for a CaseReminder with method "callback".
"""
class CaseReminderCallback(Document):
    phone_number = StringProperty()
    timestamp = DateTimeProperty()
    user_id = StringProperty()
    
    @classmethod
    def callback_exists(cls, user_id, after_timestamp):
        start_timestamp = json_format_datetime(after_timestamp)
        end_timestamp = json_format_datetime(datetime.utcnow())
        c = CaseReminderCallback.view("reminders/callbacks_by_user_timestamp"
           ,startkey = [user_id, start_timestamp]
           ,endkey = [user_id, end_timestamp]
           ,include_docs = True
        ).one()
        return (c is not None)

class CaseReminderHandler(Document):
    """
    Contains a set of rules that determine how things should fire.
    """
    domain = StringProperty()
    case_type = StringProperty()

    nickname = StringProperty()
    
    start = StringProperty()            # e.g. "edd" => create reminder on edd
                                        # | "form_started" => create reminder when form_started = 'ok'
    start_offset = IntegerProperty()    # e.g. 3 => three days after edd
    #frequency = IntegerProperty()       # e.g. 3 => every 3 days
    until = StringProperty()            # e.g. "edd" => until today > edd
                                        #    | "followup_1_complete" => until followup_1_complete = 'ok'
    #message = DictProperty()            # {"en": "Hello, {user.full_name}, you're having issues."}

    lang_property = StringProperty()    # "lang" => check for user.user_data.lang
    default_lang = StringProperty()     # lang to use in case can't find other

    method = StringProperty(choices=METHOD_CHOICES, default="sms")
    
    max_iteration_count = IntegerProperty()
    schedule_length = IntegerProperty()
    events = SchemaListProperty(CaseReminderEvent)

    @classmethod
    def get_now(cls):
        try:
            # for testing purposes only!
            return getattr(cls, 'now')
        except Exception:
            return datetime.utcnow()

    def get_reminder(self, case):
        domain = self.domain
        handler_id = self._id
        case_id = case._id
        
        return CaseReminder.view('reminders/by_domain_handler_case',
            key=[domain, handler_id, case_id],
            include_docs=True,
        ).one()

    def get_reminders(self):
        domain = self.domain
        handler_id = self._id
        return CaseReminder.view('reminders/by_domain_handler_case',
            startkey=[domain, handler_id],
            endkey=[domain, handler_id, {}],
            include_docs=True,
        ).all()

    def spawn_reminder(self, case, now):
        user = CommCareUser.get_by_user_id(case.user_id)
        local_now = CaseReminderHandler.utc_to_local(user, now)
        reminder = CaseReminder(
            domain=self.domain,
            case_id=case._id,
            handler_id=self._id,
            user_id=case.user_id,
            method=self.method,
            active=True,
            lang=self.default_lang,
            start_date=date(local_now.year,local_now.month,local_now.day),
            schedule_iteration_num=1,
            current_event_sequence_num=0,
            callback_try_count=0,
            callback_received=False
        )
        local_tmsp = datetime.combine(reminder.start_date, self.events[0].fire_time) + timedelta(days = (self.start_offset + self.events[0].day_num))
        reminder.next_fire = CaseReminderHandler.timestamp_to_utc(reminder.user, local_tmsp)
        return reminder

    @classmethod
    def utc_to_local(cls, user, timestamp):
        try:
            time_zone = timezone(user.time_zone)
            utc_datetime = pytz.utc.localize(timestamp)
            local_datetime = utc_datetime.astimezone(time_zone)
            naive_local_datetime = local_datetime.replace(tzinfo=None)
            return naive_local_datetime
        except Exception:
            return timestamp

    @classmethod
    def timestamp_to_utc(cls, user, timestamp):
        try:
            time_zone = timezone(user.time_zone)
            local_datetime = time_zone.localize(timestamp)
            utc_datetime = local_datetime.astimezone(pytz.utc)
            naive_utc_datetime = utc_datetime.replace(tzinfo=None)
            return naive_utc_datetime
        except Exception:
            return timestamp

    def move_to_next_event(self, reminder):
        reminder.current_event_sequence_num += 1
        reminder.callback_try_count = 0
        reminder.callback_received = False
        if reminder.current_event_sequence_num >= len(self.events):
            reminder.current_event_sequence_num = 0
            reminder.schedule_iteration_num += 1
            if reminder.schedule_iteration_num > self.max_iteration_count:
                reminder.active = False

    def set_next_fire(self, reminder, now):
        """
        Sets reminder.next_fire to the next allowable date after now

        This is meant to skip reminders that were just never sent i.e. because the
        reminder went dormant for a while [active=False] rather than sending one
        every minute until they're all made up for

        """
        while now >= reminder.next_fire and reminder.active:
            if (reminder.method == "callback" or reminder.method == "callback_test") and len(reminder.current_event.callback_timeout_intervals) > 0:
                if reminder.callback_received or reminder.callback_try_count >= len(reminder.current_event.callback_timeout_intervals):
                    pass
                else:
                    reminder.next_fire = reminder.next_fire + timedelta(minutes = reminder.current_event.callback_timeout_intervals[reminder.callback_try_count])
                    reminder.callback_try_count += 1
                    continue
            self.move_to_next_event(reminder)
            if reminder.active:
                next_event = reminder.current_event
                day_offset = self.start_offset + (self.schedule_length * (reminder.schedule_iteration_num - 1)) + next_event.day_num
                reminder_datetime = datetime.combine(reminder.start_date, next_event.fire_time) + timedelta(days = day_offset)
                reminder.next_fire = CaseReminderHandler.timestamp_to_utc(reminder.user, reminder_datetime)

    def should_fire(self, reminder, now):
        return now > reminder.next_fire

    def fire(self, reminder):
        if (reminder.method == "callback" or reminder.method == "callback_test") and len(reminder.current_event.callback_timeout_intervals) > 0:
            if CaseReminderCallback.callback_exists(reminder.user_id, reminder.last_fired):
                reminder.callback_received = True
                return True
        reminder.last_fired = self.get_now()
        message = reminder.current_event.message.get(reminder.lang, reminder.current_event.message[self.default_lang])
        message = Message.render(message, case=reminder.case.case_properties())
        if reminder.method == "sms" or reminder.method == "callback":
            try:
                phone_number = reminder.user.phone_number
            except Exception:
                phone_number = ''

            return send_sms(reminder.domain, reminder.user_id, phone_number, message)
        elif reminder.method == "test" or reminder.method == "callback_test":
            print(message)
            return True
        

    @classmethod
    def condition_reached(cls, case, case_property, now):
        """
        if case[case_property] is 'ok' or a date later than now then True, else False

        """
        condition = case.get_case_property(case_property)
        try: condition = string_to_datetime(condition)
        except Exception:
            pass

        if (isinstance(condition, datetime) and condition > now) or is_true_value(condition):
            return True
        else:
            return False

    def case_changed(self, case, now=None):
        now = now or self.get_now()
        reminder = self.get_reminder(case)

        if case.closed or not CommCareUser.get_by_user_id(case.user_id):
            if reminder:
                reminder.retire()
        else:
            if not reminder:
                start = case.get_case_property(self.start)
                try: start = string_to_datetime(start)
                except Exception:
                    pass
                if isinstance(start, date) or isinstance(start, datetime):
                    if isinstance(start, date):
                        start = datetime(start.year, start.month, start.day, now.hour, now.minute, now.second, now.microsecond)
                    try:
                        reminder = self.spawn_reminder(case, start)
                    except Exception:
                        if settings.DEBUG:
                            raise
                        logging.error(
                            "Case ({case._id}) submitted against "
                            "CaseReminderHandler {self.nickname} ({self._id}) "
                            "but failed to resolve case.{self.start} to a date"
                        )
                else:
                    if self.condition_reached(case, self.start, now):
                        reminder = self.spawn_reminder(case, now)
            else:
                active = not self.condition_reached(case, self.until, now)
                if active and not reminder.active:
                    # if a reminder is reactivated, sending starts over from right now
                    reminder.next_fire = now
                reminder.active = active
            if reminder:
                try:
                    reminder.lang = reminder.user.user_data.get(self.lang_property) or self.default_lang
                except Exception:
                    reminder.lang = self.default_lang
                reminder.save()

    def save(self, **params):
        super(CaseReminderHandler, self).save(**params)
        if not self.deleted():
            cases = CommCareCase.view('hqcase/open_cases',
                reduce=False,
                startkey=[self.domain],
                endkey=[self.domain, {}],
                include_docs=True,
            )
            for case in cases:
                self.case_changed(case)
    @classmethod
    def get_handlers(cls, domain, case_type=None):
        key = [domain]
        if case_type:
            key.append(case_type)
        return cls.view('reminders/handlers_by_domain_case_type',
            startkey=key,
            endkey=key + [{}],
            include_docs=True,
        )

    @classmethod
    def get_all_reminders(cls, domain=None, due_before=None):
        if due_before:
            now_json = json_format_datetime(due_before)
        else:
            now_json = {}

        # domain=None will actually get them all, so this works smoothly
        return CaseReminder.view('reminders/by_next_fire',
            startkey=[domain],
            endkey=[domain, now_json],
            include_docs=True
        ).all()
    
    @classmethod
    def fire_reminders(cls, now=None):
        now = now or cls.get_now()
        for reminder in cls.get_all_reminders(due_before=now):
            handler = reminder.handler
            if handler.fire(reminder):
                handler.set_next_fire(reminder, now)
                reminder.save()

    def retire(self):
        reminders = self.get_reminders()
        self.doc_type += "-Deleted"
        for reminder in reminders:
            print "Retiring %s" % reminder._id
            reminder.retire()
        self.save()

    def deleted(self):
        return self.doc_type != 'CaseReminderHandler'

class CaseReminder(Document):
    """
    Doesn't correlate to a single reminder, but to a way the rule is applied.
    This same object might correspond to multiple SMS messages that go out
    that are all related.
    """
    domain = StringProperty()
    case_id = StringProperty() # to a CommCareCase
    handler_id = StringProperty() # to a CaseReminderHandler
    user_id = StringProperty() # to a CommCareUser
    method = StringProperty(choices=METHOD_CHOICES)
    next_fire = DateTimeProperty()
    last_fired = DateTimeProperty()
    active = BooleanProperty(default=False)
    lang = StringProperty()
    start_date = DateProperty()
    schedule_iteration_num = IntegerProperty()
    current_event_sequence_num = IntegerProperty()
    callback_try_count = IntegerProperty()
    callback_received = BooleanProperty()

    @property
    def handler(self):
        return CaseReminderHandler.get(self.handler_id)

    @property
    def current_event(self):
        return self.handler.events[self.current_event_sequence_num]

    @property
    def case(self):
        return CommCareCase.get(self.case_id)

    @property
    def user(self):
        try:
            return CommCareUser.get_by_user_id(self.user_id)
        except Exception:
            self.retire()
            return None

    def retire(self):
        self.doc_type += "-Deleted"
        self.save()
from .signals import *
