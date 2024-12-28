from lxml import etree
from onvif import ONVIFService
from onvif.managers import BaseManager
from onvif.util import normalize_url


class CustomPullPointManager(BaseManager):
    """Manager for PullPoint."""

    async def _start(self) -> float:
        """Start the PullPoint manager.

        Returns the next renewal call at time.
        """
        device = self._device
        events_service = await device.create_events_service()

        tex_rule = etree.Element("TopicExpression")
        tex_rule.text = "tns1:RuleEngine/CellMotionDetector/Motion"
        filter_val = etree.SubElement(tex_rule, "Dialect")
        filter_val.text = "http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet"

        result = await events_service.CreatePullPointSubscription(
            {
                "Filter": {"_value_1": [tex_rule]},
                "InitialTerminationTime": device.get_next_termination_time(
                    self._interval
                ),
            }
        )
        # pylint: disable=protected-access
        device.xaddrs[
            "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription"
        ] = normalize_url(result.SubscriptionReference.Address._value_1)
        # Create subscription manager
        self._subscription = await device.create_subscription_service(
            "PullPointSubscription"
        )
        # Create the service that will be used to pull messages from the device.
        self._service = await device.create_pullpoint_service()
        if device.has_broken_relative_time(
            self._interval, result.CurrentTime, result.TerminationTime
        ):
            # If we determine the device has broken relative timestamps, we switch
            # to using absolute timestamps and renew the subscription.
            result = await self._subscription.Renew(
                device.get_next_termination_time(self._interval)
            )
        renewal_call_at = self._calculate_next_renewal_call_at(result)
        return renewal_call_at

    def get_service(self) -> ONVIFService:
        """Return the pullpoint service."""
        return self._service
