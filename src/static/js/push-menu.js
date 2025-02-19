function disableOther(s) {
    if (s !== "showLeft") {
        showLeft.classList.toggle("disabled");
    }
}

(function(s) {
    "use strict";

    // Modernizr.js or similar library handles classList support
    s.classie = {
        hasClass: function(elem, className) {
            return elem.classList.contains(className);
        },
        addClass: function(elem, className) {
            elem.classList.add(className);
        },
        removeClass: function(elem, className) {
            elem.classList.remove(className);
        },
        toggleClass: function(elem, className) {
            elem.classList.toggle(className);
        },
        has: function(elem, className) {
            return elem.classList.contains(className);
        },
        add: function(elem, className) {
            elem.classList.add(className);
        },
        remove: function(elem, className) {
            elem.classList.remove(className);
        },
        toggle: function(elem, className) {
            elem.classList.toggle(className);
        }
    };
})(window);

var menuLeft = document.getElementById("cbp-spmenu-s1");
var body = document.body;

showLeft.onclick = function() {
    this.classList.toggle("active");
    menuLeft.classList.toggle("cbp-spmenu-open");
    disableOther("showLeft");
};
